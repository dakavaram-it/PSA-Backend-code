"""
One-shot runner for scripts/add_release_for_go_stage.sql.

Applies the RELEASE_FOR_GO workflow migration to pa_track using the app's own
DB config (backend-python/.env), so no separate MySQL client is needed. Runs the
whole migration on a SINGLE connection inside one transaction (needed for the
SET @var session variables and the CREATE TEMPORARY TABLE clones), verifies the
resulting FINALISING -> RELEASE_FOR_GO -> GO_ISSUING chain, and commits ONLY if
that chain is present — otherwise it rolls back and prints what it found.

The migration itself is idempotent (guarded with NOT EXISTS), so re-running is safe.

Usage (from backend-python/):
    python scripts/apply_release_for_go.py
"""
import os
import re
import sys

# Make `app` importable, and load backend-python/.env regardless of the caller's CWD.
# config.py resolves env_file=".env" relative to the working directory, so without this
# a run from the repo root would pick up the Node BFF's root .env and fail validation.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

from sqlalchemy import text  # noqa: E402
from app.database.db import pa_track_engine  # noqa: E402

SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "add_release_for_go_stage.sql")

# Transaction control is handled here in Python, so drop these from the script.
_SKIP = re.compile(r"^\s*(START\s+TRANSACTION|COMMIT|ROLLBACK)\s*$", re.IGNORECASE)


def _statements(raw: str):
    """Strip line comments, split on ';', drop transaction-control statements."""
    no_comments = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--")
    )
    for stmt in no_comments.split(";"):
        stmt = stmt.strip()
        if stmt and not _SKIP.match(stmt):
            yield stmt


def main():
    with open(SQL_PATH, "r", encoding="utf-8") as fh:
        raw = fh.read()

    stmts = list(_statements(raw))
    conn = pa_track_engine.connect()
    trans = conn.begin()
    try:
        for stmt in stmts:
            conn.exec_driver_sql(stmt)

        rows = conn.execute(text("""
            SELECT fs.stage_code AS from_stage, a.action_code, ts.stage_code AS to_stage
            FROM workflow_transition_master t
            JOIN workflow_action_master a ON t.action_id = a.workflow_action_id
            JOIN workflow_stage_master fs ON t.from_stage_id = fs.workflow_stage_id
            JOIN workflow_stage_master ts ON t.to_stage_id = ts.workflow_stage_id
            WHERE t.is_deleted = 'N'
              AND fs.stage_code IN ('FINALISING', 'RELEASE_FOR_GO')
            ORDER BY fs.display_order
        """)).mappings().all()

        chain = {(r["from_stage"], r["action_code"], r["to_stage"]) for r in rows}
        expected = {
            ("FINALISING", "MOVE_TO_RELEASE_FOR_GO", "RELEASE_FOR_GO"),
            ("RELEASE_FOR_GO", "MOVE_TO_GO_ISSUING", "GO_ISSUING"),
        }

        print("Transitions after migration:")
        for r in rows:
            print(f"  {r['from_stage']:>16} --{r['action_code']}--> {r['to_stage']}")

        if expected.issubset(chain):
            trans.commit()
            print("\n✅ Committed. RELEASE_FOR_GO stage is in place. Restart FastAPI.")
        else:
            trans.rollback()
            missing = expected - chain
            print("\n❌ Rolled back — expected transitions missing:")
            for m in missing:
                print(f"  {m[0]} --{m[1]}--> {m[2]}")
            print(
                "\nLikely cause: the FINALISING transition in your data did not use "
                "action MOVE_TO_GO_ISSUING, so the repoint step matched nothing. "
                "Inspect workflow_transition_master for the nominated workflow and adjust "
                "add_release_for_go_stage.sql step 3 accordingly."
            )
            sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        trans.rollback()
        print(f"\n❌ Rolled back — error: {exc}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
