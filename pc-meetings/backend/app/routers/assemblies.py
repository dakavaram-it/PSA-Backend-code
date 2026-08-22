"""Assembly and parliament constituencies (the AC and PC tiers).

Both `assembly` and `parliament` live in `mytdp` itself — no cross-schema
query needed here, unlike the committee locations. 175 ACs and 25 PCs, the
full state (see the AC/PC join note in `meetings.py`: `meeting_invitee`
carries only the assembly, 175/175 match).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import access, db
from ..access import Scope
from ..auth import caller_scope

router = APIRouter(prefix="/api/assemblies", tags=["assemblies"])

_ASSEMBLIES = """
SELECT a.id, a.name, p.parliament_name
  FROM assembly a
  LEFT JOIN parliament p ON p.id = a.parliament_id
 WHERE {scoped}
 ORDER BY p.parliament_name, a.name
"""

_PARLIAMENTS = """
SELECT p.id, p.parliament_name FROM parliament p
 WHERE {scoped}
 ORDER BY p.parliament_name
"""


@router.get("")
def list_assemblies(scope: Scope = Depends(caller_scope)) -> dict[str, Any]:
    """Every assembly constituency this caller has been granted."""
    rows = db.rows(_ASSEMBLIES.format(scoped=access.assembly(scope, "a")))
    return {
        "total": len(rows),
        "rows": [
            {
                "constituencyId": r["id"],
                "constituencyName": r["name"] or "",
                "pc": r["parliament_name"] or "",
            }
            for r in rows
        ],
    }


@router.get("/parliaments")
def list_parliaments(scope: Scope = Depends(caller_scope)) -> dict[str, Any]:
    """Every parliament holding at least one assembly this caller was granted."""
    rows = db.rows(_PARLIAMENTS.format(scoped=access.parliament(scope, "p")))
    return {
        "total": len(rows),
        "rows": [
            {
                "parliamentId": r["id"],
                "parliamentName": r["parliament_name"] or "",
            }
            for r in rows
        ],
    }
