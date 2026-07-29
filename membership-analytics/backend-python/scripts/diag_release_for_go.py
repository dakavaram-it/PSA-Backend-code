"""
Read-only diagnostic for the RELEASE_FOR_GO workflow + a specific proposal.
Makes NO changes. Answers: did the migration actually commit, and did the
Assign-for-GO / step-6 move persist for a given proposal?

Usage (from backend-python/), AFTER doing "Assign for GO Issue" + "Move to Step 06"
on a proposal (say id 64) but BEFORE refreshing the browser:

    python scripts/diag_release_for_go.py 64
"""
import os
import sys

# Load backend-python/.env regardless of CWD (config.py resolves .env relative to it).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

from sqlalchemy import text  # noqa: E402
from app.database.db import pa_track_engine  # noqa: E402


def main():
    proposal_id = int(sys.argv[1]) if len(sys.argv) > 1 else None

    with pa_track_engine.connect() as conn:
        wf = conn.execute(text("""
            SELECT workflow_id FROM workflow_definition
            WHERE post_type_id=2 AND is_active='Y' AND is_deleted='N'
            ORDER BY version_no DESC LIMIT 1
        """)).mappings().first()
        print(f"Nominated workflow_id = {wf['workflow_id'] if wf else None}")

        print("\n--- Stages (code : display_order : mapped_status : terminal) ---")
        for r in conn.execute(text("""
            SELECT stage_code, display_order, mapped_proposal_status_code, is_terminal_stage
            FROM workflow_stage_master
            WHERE workflow_id=:wf AND is_deleted='N'
            ORDER BY display_order
        """), {"wf": wf["workflow_id"]}).mappings().all():
            print(f"  {r['stage_code']:>16} : {r['display_order']} : "
                  f"{r['mapped_proposal_status_code']} : {r['is_terminal_stage']}")

        print("\n--- Transitions from FINALISING / RELEASE_FOR_GO ---")
        for r in conn.execute(text("""
            SELECT fs.stage_code AS from_stage, a.action_code, ts.stage_code AS to_stage,
                   t.requires_selected_candidate, t.requires_go_details
            FROM workflow_transition_master t
            JOIN workflow_action_master a ON t.action_id=a.workflow_action_id
            JOIN workflow_stage_master fs ON t.from_stage_id=fs.workflow_stage_id
            JOIN workflow_stage_master ts ON t.to_stage_id=ts.workflow_stage_id
            WHERE t.workflow_id=:wf AND t.is_deleted='N'
              AND fs.stage_code IN ('FINALISING','RELEASE_FOR_GO','GO_ISSUING')
            ORDER BY fs.display_order
        """), {"wf": wf["workflow_id"]}).mappings().all():
            print(f"  {r['from_stage']:>16} --{r['action_code']}--> {r['to_stage']}  "
                  f"[reqSel={r['requires_selected_candidate']} reqGO={r['requires_go_details']}]")

        if proposal_id is None:
            print("\n(Pass a proposal id to inspect its persisted state.)")
            return

        print(f"\n--- Proposal {proposal_id} ---")
        p = conn.execute(text("""
            SELECT proposal_id, current_status_code
            FROM nominated_post_proposal WHERE proposal_id=:pid
        """), {"pid": proposal_id}).mappings().first()
        print(f"  proposal.current_status_code = {p['current_status_code'] if p else 'NOT FOUND'}")

        inst = conn.execute(text("""
            SELECT current_stage_code, wsm.display_order
            FROM workflow_instance wi
            LEFT JOIN workflow_stage_master wsm ON wsm.workflow_stage_id = wi.current_stage_id
            WHERE wi.reference_table_name='nominated_post_proposal'
              AND wi.reference_id=:pid AND wi.is_deleted='N'
        """), {"pid": proposal_id}).mappings().first()
        if inst:
            print(f"  workflow_instance.current_stage_code = {inst['current_stage_code']} "
                  f"(display_order {inst['display_order']})")
        else:
            print("  workflow_instance = NONE (no instance row!)")

        print("  candidates (id : status : is_selected):")
        for c in conn.execute(text("""
            SELECT proposal_candidate_id, candidate_status, is_selected
            FROM nominated_post_proposal_candidate
            WHERE proposal_id=:pid AND is_deleted='N'
            ORDER BY proposal_candidate_id
        """), {"pid": proposal_id}).mappings().all():
            print(f"    {c['proposal_candidate_id']} : {c['candidate_status']} : {c['is_selected']}")


if __name__ == "__main__":
    main()
