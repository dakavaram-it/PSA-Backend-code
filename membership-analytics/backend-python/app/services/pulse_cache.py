"""DB-backed store for computed Pulse Trend data (replaces the old file cache).

Two materialized tables in dakavara_pa, populated by a refresh:
- pulse_trend_snapshot         : the state-wide JSON snapshot (one row, key='main').
- pulse_trend_segment_decline  : per-constituency booth/caste/age/gender decline,
                                 so the drill-down page loads instantly with no
                                 on-demand compute.

Read endpoints query these; a refresh recomputes and overwrites them. Falls back
to the embedded seed (pulse_service._SEED) when the snapshot row doesn't exist yet."""
import json
import threading
from datetime import datetime, timezone

from sqlalchemy import text

from app.database.db import dakavara_session

SNAPSHOT_TABLE = "pulse_trend_snapshot"
SEGMENT_TABLE = "pulse_trend_segment_decline"
HOUSES_TABLE = "pulse_trend_constituency_houses"

_LOCK = threading.Lock()
_CACHE = {"data": None, "loaded": False}

# refresh run-state (for the status endpoint)
STATUS = {"state": "idle", "startedAt": None, "finishedAt": None,
          "error": None, "durationSec": None, "checks": None}

_DDL_SNAPSHOT = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
  snapshot_key  VARCHAR(40) NOT NULL PRIMARY KEY,
  payload       LONGTEXT NOT NULL,
  calculated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_DDL_SEGMENT = f"""
CREATE TABLE IF NOT EXISTS {SEGMENT_TABLE} (
  constituency_name VARCHAR(150) NOT NULL,
  dim               VARCHAR(16)  NOT NULL,
  seg               VARCHAR(150) NOT NULL,
  member_type       VARCHAR(40)  NOT NULL,
  n_before          INT NOT NULL DEFAULT 0,
  tdp_before        INT NOT NULL DEFAULT 0,
  n_after           INT NOT NULL DEFAULT 0,
  tdp_after         INT NOT NULL DEFAULT 0,
  KEY idx_cons_dim (constituency_name, dim)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


_DDL_HOUSES = f"""
CREATE TABLE IF NOT EXISTS {HOUSES_TABLE} (
  constituency_name VARCHAR(150) NOT NULL PRIMARY KEY,
  total    INT NOT NULL DEFAULT 0,
  stayed   INT NOT NULL DEFAULT 0,
  moved    INT NOT NULL DEFAULT 0,
  covered  INT NOT NULL DEFAULT 0,
  calculated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_tables(db):
    db.execute(text(_DDL_SNAPSHOT))
    db.execute(text(_DDL_SEGMENT))
    db.execute(text(_DDL_HOUSES))


def save_named(key: str, snapshot: dict):
    """Persist an arbitrary named JSON snapshot (e.g. 'survey') in pulse_trend_snapshot."""
    snapshot = dict(snapshot)
    snapshot["calculatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    payload = json.dumps(snapshot)
    with _LOCK:
        with dakavara_session() as db:
            ensure_tables(db)
            db.execute(text(
                f"REPLACE INTO {SNAPSHOT_TABLE} (snapshot_key, payload, calculated_at) "
                f"VALUES (:k, :p, UTC_TIMESTAMP())"), {"k": key, "p": payload})
            db.commit()
    return snapshot


def load_named(key: str):
    """Return a named snapshot dict, or None."""
    try:
        with dakavara_session() as db:
            row = db.execute(text(
                f"SELECT payload FROM {SNAPSHOT_TABLE} WHERE snapshot_key=:k"), {"k": key}).first()
            return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None


def load():
    """Return the snapshot dict from pulse_trend_snapshot, or None if not built yet."""
    if _CACHE["loaded"]:
        return _CACHE["data"]
    with _LOCK:
        data = None
        try:
            with dakavara_session() as db:
                row = db.execute(text(
                    f"SELECT payload FROM {SNAPSHOT_TABLE} WHERE snapshot_key='main'")).first()
                if row and row[0]:
                    data = json.loads(row[0])
        except Exception:
            data = None
        _CACHE["data"] = data
        _CACHE["loaded"] = True
    return _CACHE["data"]


def save(snapshot: dict):
    snapshot = dict(snapshot)
    snapshot["calculatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    payload = json.dumps(snapshot)
    with _LOCK:
        with dakavara_session() as db:
            ensure_tables(db)
            db.execute(text(
                f"REPLACE INTO {SNAPSHOT_TABLE} (snapshot_key, payload, calculated_at) "
                f"VALUES ('main', :p, UTC_TIMESTAMP())"), {"p": payload})
            db.commit()
        _CACHE["data"] = snapshot
        _CACHE["loaded"] = True
    return snapshot


def write_segments(rows):
    """Replace pulse_trend_segment_decline with `rows`:
    iterable of (constituency, dim, seg, member_type, nB, tB, nA, tA).
    DELETE then INSERT in small batches, committing each batch (small packets, short
    locks — avoids socket write-timeouts/"server gone away" on big writes)."""
    ins = text(
        f"INSERT INTO {SEGMENT_TABLE} "
        f"(constituency_name, dim, seg, member_type, n_before, tdp_before, n_after, tdp_after) "
        f"VALUES (:cn, :dim, :seg, :mt, :nB, :tB, :nA, :tA)")
    rows = list(rows)
    total = 0
    with _LOCK:
        with dakavara_session() as db:
            ensure_tables(db)
            db.execute(text(f"DELETE FROM {SEGMENT_TABLE}"))
            db.commit()
            batch = []
            for r in rows:
                batch.append({"cn": r[0], "dim": r[1], "seg": r[2], "mt": r[3],
                              "nB": r[4], "tB": r[5], "nA": r[6], "tA": r[7]})
                if len(batch) >= 800:
                    db.execute(ins, batch); db.commit(); total += len(batch); batch = []
            if batch:
                db.execute(ins, batch); db.commit(); total += len(batch)
    return total


def write_constituency(name, dims, houses):
    """Lazy materialization: persist one constituency's segment rows + houses so the
    next open is instant. Replaces just this constituency's rows (small, fast)."""
    seg_rows = [{"cn": name, "dim": dim, "seg": r["seg"], "mt": r["mt"],
                 "nB": r["nB"], "tB": r["tB"], "nA": r["nA"], "tA": r["tA"]}
                for dim, rows in dims.items() for r in rows]
    with _LOCK:
        with dakavara_session() as db:
            ensure_tables(db)
            db.execute(text(f"DELETE FROM {SEGMENT_TABLE} WHERE constituency_name = :c"), {"c": name})
            if seg_rows:
                db.execute(text(
                    f"INSERT INTO {SEGMENT_TABLE} (constituency_name, dim, seg, member_type, "
                    f"n_before, tdp_before, n_after, tdp_after) "
                    f"VALUES (:cn, :dim, :seg, :mt, :nB, :tB, :nA, :tA)"), seg_rows)
            # Only write houses when we actually have them — the lazy per-seat path
            # returns 0 (households come from the bulk roll join), so don't clobber it.
            if houses.get("total") or houses.get("covered"):
                db.execute(text(
                    f"REPLACE INTO {HOUSES_TABLE} (constituency_name, total, stayed, moved, covered, calculated_at) "
                    f"VALUES (:c, :t, :s, :m, :cv, UTC_TIMESTAMP())"),
                    {"c": name, "t": houses["total"], "s": houses["stayed"],
                     "m": houses["moved"], "cv": houses["covered"]})
            db.commit()


def write_all_houses(houses_by_cn):
    """Replace the whole houses table from {CONSTITUENCY: {stayed,moved,total,covered}}."""
    rows = [{"c": cn, "t": h["total"], "s": h["stayed"], "m": h["moved"], "cv": h["covered"]}
            for cn, h in houses_by_cn.items()]
    with _LOCK:
        with dakavara_session() as db:
            ensure_tables(db)
            db.execute(text(f"DELETE FROM {HOUSES_TABLE}"))
            if rows:
                db.execute(text(
                    f"INSERT INTO {HOUSES_TABLE} (constituency_name, total, stayed, moved, covered, calculated_at) "
                    f"VALUES (:c, :t, :s, :m, :cv, UTC_TIMESTAMP())"), rows)
            db.commit()
    return len(rows)


def read_constituency(name):
    """Materialized drill-down for one constituency -> {dims, houses}, or None if not
    built yet (caller computes live then writes it)."""
    try:
        with dakavara_session() as db:
            seg = db.execute(text(
                f"SELECT dim, seg, member_type mt, n_before nB, tdp_before tB, "
                f"n_after nA, tdp_after tA FROM {SEGMENT_TABLE} WHERE constituency_name = :c"),
                {"c": name}).mappings().all()
            if not seg:
                return None
            hr = db.execute(text(
                f"SELECT total, stayed, moved, covered FROM {HOUSES_TABLE} WHERE constituency_name = :c"),
                {"c": name}).mappings().first()
    except Exception:
        return None
    dims = {}
    for r in seg:
        dims.setdefault(r["dim"], []).append(
            {"seg": r["seg"], "mt": r["mt"], "nB": r["nB"], "tB": r["tB"], "nA": r["nA"], "tA": r["tA"]})
    houses = dict(hr) if hr else {"total": 0, "stayed": 0, "moved": 0, "covered": 0}
    return {"dims": dims, "houses": houses}
