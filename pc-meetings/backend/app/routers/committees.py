"""Mandal / Town / Division committee locations.

`tdp_committee` lives in the `dakavara_pa` schema on the same RDS host as
`mytdp`; the query below is schema-qualified rather than switching databases.
Levels 5 (tehsil), 7 (local_election_body) and 9 (ward-as-constituency) are
the three location kinds the meetings side already folds into one
"Mandal / Town / Division" tier (`adapt.LEVEL_BY_NAME`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import db

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


@router.get("/mandal-town-division")
def mandal_town_division() -> dict[str, Any]:
    """Every enrolled Mandal/Town/Division committee, with its total count."""
    rows = db.rows(_LOCATIONS)
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
