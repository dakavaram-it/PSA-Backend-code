"""AC-level reference data for the SIR report, read from **dakavara_pa**.

The SIR submission data lives in `mytdp` (sir_form_counts), but the master
geography — Parliament/Assembly names, the full booth roll per AC and the voter
totals — lives in dakavara_pa. The two databases are on different RDS instances
and do NOT share a booth-id space, so they can't be SQL-joined. Instead we
aggregate dakavara_pa to the **AC level** here, and the service merges it with
the mytdp SIR metrics by **AC name** (verified 145/145 match).

Per the requirement, dakavara_pa is the preferred source for this reference data.

Source tables:
    PC_AC_CLUSTER_UNIT_BOOTH_MAPPING  -- PC_NAME, AC_NAME, AC_ID, the booth roll
    booth                             -- booth.total_voters (booth_id is unique)

The result is small (one row per AC, ~175 rows) and static, so it's cached in
process for `_TTL_SECONDS` to keep the report fast.
"""
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

_TTL_SECONDS = 3600
_CACHE = {"at": 0.0, "data": None}


class SirReferenceRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    def ac_reference(self, force=False):
        """Return {AC_NAME_UPPER: {acName, pcName, acNo, totalBooths, totalVoters}}.

        Cached for an hour — the electoral roll changes at publication cadence,
        not per request.
        """
        now = time.time()
        if not force and _CACHE["data"] is not None and now - _CACHE["at"] < _TTL_SECONDS:
            return _CACHE["data"]

        rows = self.db.execute(text("""
            SELECT UPPER(TRIM(m.AC_NAME))        AS acKey,
                   MAX(m.AC_NAME)                AS acName,
                   MAX(m.PC_NAME)                AS pcName,
                   MAX(m.AC_ID)                  AS acNo,
                   COUNT(DISTINCT m.BOOTH_ID)    AS totalBooths,
                   COALESCE(SUM(b.total_voters), 0) AS totalVoters
            FROM PC_AC_CLUSTER_UNIT_BOOTH_MAPPING m
            LEFT JOIN booth b ON b.booth_id = m.BOOTH_ID
            GROUP BY UPPER(TRIM(m.AC_NAME))
        """)).mappings().all()

        data = {}
        for r in rows:
            data[r["acKey"]] = {
                "acName": r["acName"],
                "pcName": r["pcName"],
                "acNo": int(r["acNo"]) if r["acNo"] is not None else None,
                "totalBooths": int(r["totalBooths"] or 0),
                "totalVoters": int(r["totalVoters"] or 0),
            }
        _CACHE.update(at=now, data=data)
        return data
