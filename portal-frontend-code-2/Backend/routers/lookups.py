# Backend/routers/lookups.py — the picklists Dashboard 2's filters are built from.
#
# The first five are the reference tables the screen's own vocabulary comes from, returned
# whole: they are tens of rows, they never change during a session, and filtering them
# server-side would only mean a round trip per filter change.
#
# The last two are the location pickers behind the (userLocationLevelId,
# userLocationLevelValuesStr) pair every other endpoint takes — /parliaments feeds level 4,
# /assemblies feeds level 5.
from typing import List, Optional

from fastapi import APIRouter, Query

from db import run
from queries import (
    ASSEMBLIES_SELECT,
    ELECTION_TYPES_SELECT,
    MAIN_ELECTION_TYPES_SELECT,
    PARLIAMENTS_SELECT,
    RESERVATIONS_SELECT,
    ROLES_SELECT,
    STATUSES_SELECT,
)
from scope import STATE_ID, placeholders

router = APIRouter(prefix="/api/dashboard2", tags=["dashboard2-lookups"])


@router.get("/mainElectionTypes")
def main_election_types():
    """The five body groups Dashboard 2 splits its table into."""
    return run(MAIN_ELECTION_TYPES_SELECT)


@router.get("/electionTypes")
def election_types():
    """Every proposal_election_type, active or not, each carrying the body it belongs to.

    Inactive types are included rather than filtered: GMC Ward is is_active 'N' and has no
    main_election_type_id, and a caller that hides it silently will not notice when a live
    position turns up under it.
    """
    return run(ELECTION_TYPES_SELECT)


@router.get("/roles")
def roles():
    return run(ROLES_SELECT)


@router.get("/statuses")
def statuses():
    """proposal_status: 1 Proposed, 2 Confirmed. Shortlisted was dropped from the table and
    Confirmed moved down from 3 to 2 with it, so any id 3 anywhere is stale."""
    return run(STATUSES_SELECT)


@router.get("/reservations")
def reservations():
    return run(RESERVATIONS_SELECT)


@router.get("/parliaments")
def parliaments():
    """The level-4 picklist — parliament constituencies in the state."""
    return run(PARLIAMENTS_SELECT, (STATE_ID,))


@router.get("/assemblies")
def assemblies(
    parliamentId: Optional[List[str]] = Query(
        None,
        description=(
            "Narrow to the assemblies under these parliament constituencies. Repeat the "
            "parameter or send one comma-separated string."
        ),
    ),
):
    """The level-5 picklist — assembly constituencies, optionally under given parliaments."""
    ids = []
    for value in parliamentId or []:
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    if not ids:
        return run(ASSEMBLIES_SELECT.format(filter=""), (STATE_ID,))
    return run(
        ASSEMBLIES_SELECT.format(filter=f"AND C.parliament_id IN ({placeholders(ids)})"),
        (STATE_ID, *ids),
    )
