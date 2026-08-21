# Backend/scope.py — the (userLocationLevelId, userLocationLevelValuesStr) pair every
# Dashboard 2 endpoint takes, turned into the one thing the SQL actually needs: a list of
# assembly constituency_ids.
#
# There is no login on this backend, so the caller states its own scope on every request.
# That is the whole access story here: whatever the caller sends is what it gets. Do not
# read this as authorisation — it is a filter, and a caller may widen it at will.
#
#   userLocationLevelId = 5   values are assembly constituency_ids     -> used as-is
#   userLocationLevelId = 4   values are parliament constituency_ids   -> expanded to the
#                                                                        assemblies whose
#                                                                        parliament_id
#                                                                        points at them
#   userLocationLevelId = None (or no values at all)                   -> the whole state
#
# State scope is represented as `None`, not as "every assembly id": with 175 assemblies in
# Andhra Pradesh the IN-list form would be both slower and equivalent, and every query in
# queries.py therefore drops its assembly predicate entirely when the scope is None rather
# than testing 175 values per row.
#
# The nearest thing in the repo is ../../portal-frontend-code/Backend/main.py's
# user_access_assemblies(), which answers the same question from a session user id. This
# backend has no session, so none of it is reusable.
from typing import List, Optional

from fastapi import HTTPException, Query

from db import run

# election_scope.election_type_id is what separates the three kinds of `constituency` row.
# 2 is an assembly, 1 is a parliament — the same test ../../portal-frontend-code/Backend/main.py
# uses in getAssemblyConstituenciesInAState and user_access_assemblies.
ASSEMBLY_ELECTION_TYPE_ID = 2
PARLIAMENT_ELECTION_TYPE_ID = 1

# Andhra Pradesh. The other projects hardcode this the same way; the portal is a
# single-state deployment.
STATE_ID = 1

LEVEL_ASSEMBLY = 5
LEVEL_PARLIAMENT = 4


def placeholders(values) -> str:
    return ", ".join(["%s"] * len(values))


def _split(values: Optional[List[str]]) -> List[int]:
    """Flatten the array parameter into ints.

    Accepts both shapes so callers do not have to care which one they send:
    repeated `?userLocationLevelValuesStr=111&userLocationLevelValuesStr=127` (what
    Swagger's array input produces) and the single comma-joined
    `?userLocationLevelValuesStr=111,127,133` string the PSA webservices use.
    """
    out: List[int] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                raise HTTPException(
                    400, f"userLocationLevelValuesStr must be numeric ids; got {part!r}"
                )
            out.append(int(part))
    # Deduplicated: a repeated id would multiply nothing (the SQL uses IN), but it would
    # show up in the echoed scope and read as a data problem.
    return sorted(set(out))


class Scope:
    """Resolved location scope. `assembly_ids is None` means state-wide."""

    def __init__(
        self,
        level_id: Optional[int],
        values: List[int],
        assembly_ids: Optional[List[int]],
    ):
        self.level_id = level_id
        self.values = values
        self.assembly_ids = assembly_ids

    @property
    def state_wide(self) -> bool:
        return self.assembly_ids is None

    def describe(self) -> dict:
        """Echoed back on every response so a caller can see what its scope resolved to —
        the difference between "no rows because nothing is configured" and "no rows because
        the ids named nothing" is otherwise invisible."""
        return {
            "userLocationLevelId": self.level_id,
            "userLocationLevelValues": self.values,
            "resolvedAssemblyCount": None if self.state_wide else len(self.assembly_ids),
            "stateWide": self.state_wide,
        }


def resolve_scope(
    userLocationLevelId: Optional[int] = Query(
        None,
        description="5 = Assembly, 4 = Parliament, omitted/null = whole state.",
    ),
    userLocationLevelValuesStr: Optional[List[str]] = Query(
        None,
        description=(
            "Location ids for the level above. Send as an array "
            "(?userLocationLevelValuesStr=111&userLocationLevelValuesStr=127) or as one "
            "comma-separated string (?userLocationLevelValuesStr=111,127). Ignored when "
            "userLocationLevelId is null."
        ),
    ),
) -> Scope:
    values = _split(userLocationLevelValuesStr)

    if userLocationLevelId is None or not values:
        return Scope(userLocationLevelId, values, None)

    if userLocationLevelId == LEVEL_ASSEMBLY:
        rows = run(
            "SELECT C.constituency_id FROM constituency C "
            "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
            f"WHERE ES.election_type_id = {ASSEMBLY_ELECTION_TYPE_ID} "
            "AND C.deform_date IS NULL "
            f"AND C.constituency_id IN ({placeholders(values)})",
            tuple(values),
        )
    elif userLocationLevelId == LEVEL_PARLIAMENT:
        rows = run(
            "SELECT C.constituency_id FROM constituency C "
            "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
            f"WHERE ES.election_type_id = {ASSEMBLY_ELECTION_TYPE_ID} "
            "AND C.deform_date IS NULL "
            f"AND C.parliament_id IN ({placeholders(values)})",
            tuple(values),
        )
    else:
        raise HTTPException(
            400,
            f"userLocationLevelId {userLocationLevelId} is not supported. "
            f"Use {LEVEL_ASSEMBLY} (Assembly), {LEVEL_PARLIAMENT} (Parliament), "
            "or omit it for the whole state.",
        )

    assembly_ids = [int(r["constituency_id"]) for r in rows]
    if not assembly_ids:
        # An empty resolution is not the same as state-wide, and must not silently widen
        # into it. An impossible scope is kept impossible, so the caller sees zero rows.
        assembly_ids = [-1]
    return Scope(userLocationLevelId, values, assembly_ids)
