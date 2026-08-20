"""Assembly and parliament constituencies (the AC and PC tiers).

Both `assembly` and `parliament` live in `mytdp` itself — no cross-schema
query needed here, unlike the committee locations. 175 ACs and 25 PCs, the
full state (see the AC/PC join note in `meetings.py`: `meeting_invitee`
carries only the assembly, 175/175 match).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import db

router = APIRouter(prefix="/api/assemblies", tags=["assemblies"])

_ASSEMBLIES = """
SELECT a.id, a.name, p.parliament_name
  FROM assembly a
  LEFT JOIN parliament p ON p.id = a.parliament_id
 ORDER BY p.parliament_name, a.name
"""

_PARLIAMENTS = """
SELECT id, parliament_name FROM parliament ORDER BY parliament_name
"""


@router.get("")
def list_assemblies() -> dict[str, Any]:
    """Every assembly constituency in the state, with its total count."""
    rows = db.rows(_ASSEMBLIES)
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
def list_parliaments() -> dict[str, Any]:
    """Every parliament constituency in the state, with its total count."""
    rows = db.rows(_PARLIAMENTS)
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
