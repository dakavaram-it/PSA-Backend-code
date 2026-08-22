"""Units (the Unit tier) — booths grouped by their assembly and physical unit.

`booth`, `assembly` and `unit` all live in `mytdp`. A booth's `publication_id`
scopes it to the live electoral roll (`config.UNIT_PUBLICATION_ID`); grouping
by (assembly, unit) reproduces the same count as `COUNT(DISTINCT unit.id)` —
no fan-out, unlike the committee locations.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import access, config, db
from ..access import Scope
from ..auth import caller_scope

router = APIRouter(prefix="/api/units", tags=["units"])

_UNITS = """
SELECT AC.id ACID, AC.code ASSEMBLY, UT.id UTID, UT.code UNIT
  FROM booth B
  JOIN assembly AC ON B.assembly_id = AC.id AND B.publication_id = %s
  JOIN unit UT ON B.unit_id = UT.id
 WHERE {scoped}
 GROUP BY AC.id, UT.id
 ORDER BY AC.code, UT.code
"""


@router.get("")
def list_units(scope: Scope = Depends(caller_scope)) -> dict[str, Any]:
    """Every unit inside the assemblies this caller has been granted."""
    rows = db.rows(
        _UNITS.format(scoped=access.booth(scope, "B")), (config.UNIT_PUBLICATION_ID,)
    )
    return {
        "total": len(rows),
        "rows": [
            {
                "assemblyId": r["ACID"],
                "assemblyCode": r["ASSEMBLY"] or "",
                "unitId": r["UTID"],
                "unitCode": r["UNIT"] or "",
            }
            for r in rows
        ],
    }
