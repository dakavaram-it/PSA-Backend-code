"""Mandal / Town / Division committee locations.

`tdp_committee` lives in the `dakavara_pa` schema on the same RDS host as
`mytdp`; the query below is schema-qualified rather than switching databases.
Levels 5 (tehsil), 7 (local_election_body) and 9 (ward-as-constituency) are
the three location kinds the meetings side already folds into one
"Mandal / Town / Division" tier (`adapt.LEVEL_BY_NAME`).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends

from .. import db
from ..access import Scope
from ..auth import caller_scope

router = APIRouter(prefix="/api/committees", tags=["committees"])

_LOCATIONS = """
SELECT
    CASE WHEN TCL.tdp_committee_level_id = 5 THEN T.tehsil_id
         WHEN TCL.tdp_committee_level_id = 7 THEN L.local_election_body_id
         WHEN TCL.tdp_committee_level_id = 9 THEN W.constituency_id END AS location_id,
    CASE WHEN TCL.tdp_committee_level_id = 5 THEN T.tehsil_name
         WHEN TCL.tdp_committee_level_id = 7 THEN L.name
         WHEN TCL.tdp_committee_level_id = 9 THEN W.name END AS location_name,
    TCL.tdp_committee_level, C.constituency_id, C.name AS constituency_name
  FROM dakavara_pa.tdp_committee TC
  JOIN dakavara_pa.user_address UA ON TC.address_id = UA.user_address_id
  JOIN dakavara_pa.constituency C ON UA.constituency_id = C.constituency_id
  JOIN dakavara_pa.tdp_committee_level TCL ON TC.tdp_committee_level_id = TCL.tdp_committee_level_id
  LEFT OUTER JOIN dakavara_pa.tehsil T ON UA.tehsil_id = T.tehsil_id
  LEFT OUTER JOIN dakavara_pa.local_election_body L ON UA.local_election_body = L.local_election_body_id
  LEFT OUTER JOIN dakavara_pa.constituency W ON UA.ward = W.constituency_id
 WHERE TC.tdp_committee_enrollment_id = 4
   AND TC.tdp_committee_level_id IN (5, 7, 9)
   AND TC.tdp_basic_committee_id = 1
 ORDER BY C.name, location_name
"""

# Seven joins across two schemas, and the answer changes about as often as a committee is
# enrolled — but seven call sites read it, two of them on every meetings-list load (the
# App-side and PC-side "not updated" counts, which `db.parallel` runs at the same time),
# so the same roster was being rebuilt several times per page. Memoised behind a lock so
# the concurrent pair costs one query rather than two, and every caller treats the rows as
# read-only.
_TTL_SECONDS = int(os.getenv("COMMITTEE_LOCATIONS_CACHE_SECONDS", "300"))
_cache: tuple[float, list[dict[str, Any]]] | None = None
_cache_lock = threading.Lock()


def locations() -> list[dict[str, Any]]:
    """Every enrolled Mandal/Town/Division committee row. Do not mutate — shared."""
    global _cache
    now = time.monotonic()
    hit = _cache
    if hit is not None and hit[0] > now:
        return hit[1]
    with _cache_lock:
        hit = _cache
        if hit is not None and hit[0] > now:
            return hit[1]
        rows = db.rows(_LOCATIONS)
        _cache = (now + _TTL_SECONDS, rows)
        return rows


@router.get("/mandal-town-division")
def mandal_town_division(scope: Scope = Depends(caller_scope)) -> dict[str, Any]:
    """Every enrolled Mandal/Town/Division committee inside the assemblies this
    caller has been granted.

    Filtered here rather than in the query: `locations()` is memoised and shared
    with the meetings routes, so it stays the whole roster and each caller takes
    their slice of it. `constituency_id` is the assembly — the same id
    `mytdp.assembly.id` uses, see `access.py`.
    """
    allowed = {str(i) for i in (scope.ids or ())}
    rows = [
        r for r in locations()
        if scope.unrestricted or str(r["constituency_id"]) in allowed
    ]
    return {
        "total": len(rows),
        "rows": [
            {
                "locationId": r["location_id"],
                "locationName": r["location_name"] or "",
                "level": r["tdp_committee_level"] or "",
                "constituencyId": r["constituency_id"],
                "constituencyName": r["constituency_name"] or "",
            }
            for r in rows
        ],
    }
