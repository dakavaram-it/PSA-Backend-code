# Backend/routers/dashboard.py — the six read endpoints Dashboard 2's screens are built on.
#
#   GET /api/dashboard2/pipeline            the six-step header, one bar per step
#   GET /api/dashboard2/positionSummary     the main table: body x post x counters
#   GET /api/dashboard2/geoBreakdown        one post, split by parliament and by assembly
#   GET /api/dashboard2/reservationSummary  one post, split by reservation
#   GET /api/dashboard2/locations           one post's locations, paged and filterable
#   GET /api/dashboard2/locationCandidates  the names on one location, side by side
#
# Every one of them takes the same scope pair (userLocationLevelId,
# userLocationLevelValuesStr) through scope.resolve_scope — see scope.py for what the pair
# means and why it is not access control.
#
# Read-only. Dashboard 2 reports on the workflow; the writes stay in
# ../../portal-frontend-code, which already owns assignProposalCandidate and the eligibility
# rules behind it. Duplicating those here would mean two places to keep a 409 correct.
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db import read_cursor, run
from queries import (
    GEO_BY_ASSEMBLY_SELECT,
    GEO_BY_PARLIAMENT_SELECT,
    GEO_JOIN,
    LOCATION_CANDIDATES_SELECT,
    LOCATIONS_COUNT_SELECT,
    LOCATIONS_SELECT,
    POSITION_SUMMARY_SELECT,
    RESERVATION_SUMMARY_SELECT,
    STAGE_EXPR,
)
from scope import Scope, placeholders, resolve_scope

router = APIRouter(prefix="/api/dashboard2", tags=["dashboard2"])

# Dashboard 2's own ladder, named exactly as the screen names it.
STAGE_NAMES = [
    "Not started",
    "Proposal received",
    "Confirmed",
    "Nomination filed",
    "Door to Door done",
    "Door to Door - 2 done",
    "Result declared",
]
MAX_DERIVABLE_STAGE = 3

# Stages 4-6 have no table in dakavara_pa. Rather than omit the fields — which would make
# the frontend branch on their absence — every counter block carries them as zeros, and
# `stagesUnavailable` on the response says so out loud. Delete this constant and the two
# lines that merge it the day the tables exist.
EMPTY_STAGE_FIELDS = {
    "door_to_door": 0,
    "door_to_door_2": 0,
    "declared": 0,
    "won": 0,
    "lost": 0,
    "total_houses": 0,
    "houses_visited": 0,
    "houses_visited_2": 0,
    "houses_pending": 0,
    "houses_pending_2": 0,
}
STAGES_UNAVAILABLE = [
    "door_to_door",
    "door_to_door_2",
    "declared",
    "won",
    "lost",
    "total_houses",
    "houses_visited",
    "houses_visited_2",
]
UNAVAILABLE_NOTE = (
    "Door to Door, Door to Door - 2 and Result have no source table in dakavara_pa. "
    "These fields are reported as 0 and are not derived from data."
)

ZERO_ROW = {
    "total_locations": 0,
    "started": 0,
    "confirmed": 0,
    "nominated": 0,
    "proposed_names": 0,
    "max_positions": 0,
    "max_proposals": 0,
}


def counters(row: dict) -> dict:
    """One row's counter block, in the shape every endpoint returns."""
    total = int(row["total_locations"])
    started = int(row["started"] or 0)
    confirmed = int(row["confirmed"] or 0)
    nominated = int(row["nominated"] or 0)
    block = {
        "total_locations": total,
        "not_started": total - started,
        "started": started,
        "confirmed": confirmed,
        "nominated": nominated,
        # What is waiting AT each stage — the "Pending" column Dashboard 2 puts beside
        # every step. Derived here rather than in the browser so the two cannot disagree.
        "pending_confirmation": started - confirmed,
        "pending_nomination": confirmed - nominated,
        "proposed_names": int(row["proposed_names"] or 0),
        "max_positions": int(row["max_positions"] or 0),
        "max_proposals": int(row["max_proposals"] or 0),
    }
    block.update(EMPTY_STAGE_FIELDS)
    return block


def _sum_counters(rows: List[dict]) -> dict:
    out = {k: 0 for k in counters(ZERO_ROW)}
    for row in rows:
        for key, value in counters(row).items():
            out[key] += value
    return out


# --- filter fragments ------------------------------------------------------
# Each returns (sql_fragment, args). They are appended to the query in the same order the
# {placeholders} appear in queries.py, which is what keeps the %s args lined up.


def scope_filter(scope: Scope):
    """The assembly predicate, and the join that makes AC.constituency_id available.

    Returns (joins, where, args). State-wide scope contributes neither: with no ids to test
    there is nothing to filter, and skipping the join to the attributed assembly is what
    keeps the state-wide summary off ASSEMBLY_EXPR's three correlated subqueries entirely.
    """
    if scope.state_wide:
        return "", "", ()
    ids = scope.assembly_ids
    return GEO_JOIN, f"AND AC.constituency_id IN ({placeholders(ids)})", tuple(ids)


def position_filter(
    main_election_type_id: Optional[int],
    proposal_election_type_id: int,
    proposal_role_id: int,
):
    sql = "AND PET.proposal_election_type_id = %s AND PR.proposal_role_id = %s"
    args = [proposal_election_type_id, proposal_role_id]
    if main_election_type_id is not None:
        sql += " AND MET.main_election_type_id = %s"
        args.append(main_election_type_id)
    return sql, tuple(args)


def stage_filter(stage: Optional[int]):
    if stage is None:
        return "", ()
    if stage > MAX_DERIVABLE_STAGE:
        # Not an error: the stage is real on the screen, there is just no row that can be
        # at it. Matching nothing is the honest answer, and it keeps the caller's filter
        # chips working without a special case.
        return "AND 1 = 0", ()
    return f"AND ({STAGE_EXPR}) = %s", (stage,)


def reservation_filter(reservation_type: Optional[str]):
    if reservation_type is None:
        return "", ()
    if reservation_type.upper() == "NONE":
        return "AND CR.reservation_type IS NULL", ()
    return "AND CR.reservation_type = %s", (reservation_type,)


def position_row(row: dict) -> dict:
    return {
        "main_election_type_id": row["main_election_type_id"],
        "main_election_type": row["main_election_type"],
        "proposal_election_type_id": row["proposal_election_type_id"],
        "election_type": row["election_type"],
        "proposal_role_id": row["proposal_role_id"],
        "role_name": row["role_name"],
        **counters(row),
    }


def _summary_rows(scope: Scope) -> List[dict]:
    joins, where, args = scope_filter(scope)
    return run(POSITION_SUMMARY_SELECT.format(joins=joins, scope=where), args)


def _candidates_for(position_ids, cur=None) -> dict:
    if not position_ids:
        return {}
    rows = run(
        LOCATION_CANDIDATES_SELECT.format(ids=placeholders(position_ids)),
        tuple(position_ids),
        cur=cur,
    )
    out: dict = {pid: [] for pid in position_ids}
    for row in rows:
        out.setdefault(row["proposal_position_id"], []).append(row)
    return out


# --- endpoints -------------------------------------------------------------


@router.get("/positionSummary")
def position_summary(scope: Scope = Depends(resolve_scope)):
    """Dashboard 2's main table — every post in every body, with its counters.

    One row per (main_election_type, proposal_election_type, proposal_role). That triple is
    the post's identity: role 5 (Corporator) serves both Municipal Ward and Corporation
    Ward, so grouping on the role alone would merge two of the screen's rows into one.
    """
    rows = _summary_rows(scope)
    return {
        "scope": scope.describe(),
        "stagesUnavailable": STAGES_UNAVAILABLE,
        "note": UNAVAILABLE_NOTE,
        "totals": _sum_counters(rows),
        "positions": [position_row(r) for r in rows],
    }


@router.get("/pipeline")
def pipeline(scope: Scope = Depends(resolve_scope)):
    """The six progress bars across the top of Dashboard 2.

    Each step is a ratio of the step before it — proposals out of locations, confirmations
    out of proposals, and so on — which is exactly how the screen draws them. Served off the
    same aggregate as /positionSummary so a bar can never disagree with the table under it.
    """
    totals = _sum_counters(_summary_rows(scope))
    steps = [
        ("Proposal", "started", "total_locations"),
        ("Confirmation", "confirmed", "started"),
        ("Nomination", "nominated", "confirmed"),
        ("Door to Door", "door_to_door", "nominated"),
        ("Door to Door - 2", "door_to_door_2", "door_to_door"),
        ("Result", "declared", "door_to_door_2"),
    ]
    return {
        "scope": scope.describe(),
        "stagesUnavailable": STAGES_UNAVAILABLE,
        "note": UNAVAILABLE_NOTE,
        "totals": totals,
        "steps": [
            {
                "step": i,
                "name": name,
                "done": totals[num],
                "of": totals[den],
                "percent": round(totals[num] * 100 / totals[den]) if totals[den] else 0,
                "available": i < MAX_DERIVABLE_STAGE,
            }
            for i, (name, num, den) in enumerate(steps)
        ],
    }


@router.get("/geoBreakdown")
def geo_breakdown(
    proposalElectionTypeId: int,
    proposalRoleId: int,
    mainElectionTypeId: Optional[int] = None,
    parliamentId: Optional[int] = Query(
        None, description="Narrow the assembly half to one parliament constituency."
    ),
    scope: Scope = Depends(resolve_scope),
):
    """One post's counters split by parliament, and again by assembly.

    Both halves add back up to that post's row in /positionSummary. That holds because every
    position is attributed to exactly one assembly — see queries.ASSEMBLY_EXPR, which also
    documents the corner that rule cuts for whole-body and district positions.
    """
    pos_sql, pos_args = position_filter(
        mainElectionTypeId, proposalElectionTypeId, proposalRoleId
    )
    _, scope_sql, scope_args = scope_filter(scope)
    par_sql, par_args = (
        ("AND AC.parliament_id = %s", (parliamentId,)) if parliamentId else ("", ())
    )

    with read_cursor() as cur:
        parliaments = run(
            GEO_BY_PARLIAMENT_SELECT.format(position=pos_sql, scope=scope_sql),
            (*pos_args, *scope_args),
            cur=cur,
        )
        assemblies = run(
            GEO_BY_ASSEMBLY_SELECT.format(
                position=pos_sql, scope=scope_sql, parliament=par_sql
            ),
            (*pos_args, *scope_args, *par_args),
            cur=cur,
        )

    return {
        "scope": scope.describe(),
        "stagesUnavailable": STAGES_UNAVAILABLE,
        "position": {
            "main_election_type_id": mainElectionTypeId,
            "proposal_election_type_id": proposalElectionTypeId,
            "proposal_role_id": proposalRoleId,
        },
        "parliaments": [
            {
                "parliament_id": r["parliament_id"],
                "parliament_name": r["parliament_name"],
                **counters(r),
            }
            for r in parliaments
        ],
        "assemblies": [
            {
                "assembly_id": r["assembly_id"],
                "assembly_name": r["assembly_name"],
                "parliament_id": r["parliament_id"],
                "parliament_name": r["parliament_name"],
                **counters(r),
            }
            for r in assemblies
        ],
    }


@router.get("/reservationSummary")
def reservation_summary(
    proposalElectionTypeId: int,
    proposalRoleId: int,
    mainElectionTypeId: Optional[int] = None,
    scope: Scope = Depends(resolve_scope),
):
    """Dashboard 2's reservation cards for one post.

    Every reservation actually configured on that post's positions, plus a bucket for the
    ones with none — reported as reservation_type null, never folded into GENERAL, since
    "unreserved" and "not configured yet" are different states to fix.
    """
    pos_sql, pos_args = position_filter(
        mainElectionTypeId, proposalElectionTypeId, proposalRoleId
    )
    joins, scope_sql, scope_args = scope_filter(scope)
    rows = run(
        RESERVATION_SUMMARY_SELECT.format(joins=joins, position=pos_sql, scope=scope_sql),
        (*pos_args, *scope_args),
    )
    return {
        "scope": scope.describe(),
        "stagesUnavailable": STAGES_UNAVAILABLE,
        "totals": _sum_counters(rows),
        "reservations": [
            {
                "constituency_reservation_id": r["constituency_reservation_id"],
                "reservation_type": r["reservation_type"],
                "caste_category_id": r["caste_category_id"],
                "gender": r["gender"],
                **counters(r),
            }
            for r in rows
        ],
    }


@router.get("/locations")
def locations(
    proposalElectionTypeId: int,
    proposalRoleId: int,
    mainElectionTypeId: Optional[int] = None,
    stage: Optional[int] = Query(
        None, ge=0, le=6, description="Keep only locations at this stage (0-6)."
    ),
    reservationType: Optional[str] = Query(
        None, description="A reservation_type, or the literal 'NONE' for unreserved."
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    includeCandidates: bool = Query(
        True, description="Attach each location's proposed names. One extra query per page."
    ),
    scope: Scope = Depends(resolve_scope),
):
    """One post's locations — Dashboard 2's location list.

    A location is one proposal_position row, so its reservation, its names and its stage all
    come off the same row the counters were summed from.

    Paged, because a single post can be 12,451 locations (MPTC state-wide). The names are
    fetched for the page as a whole (one IN-list query), never per row: the comparison table
    needs them all at once and a per-row lookup would be one query per location.
    """
    pos_sql, pos_args = position_filter(
        mainElectionTypeId, proposalElectionTypeId, proposalRoleId
    )
    _, scope_sql, scope_args = scope_filter(scope)
    stage_sql, stage_args = stage_filter(stage)
    res_sql, res_args = reservation_filter(reservationType)
    filter_args = (*pos_args, *scope_args, *stage_args, *res_args)
    fmt = dict(position=pos_sql, scope=scope_sql, stage=stage_sql, reservation=res_sql)

    with read_cursor() as cur:
        total = int(
            run(LOCATIONS_COUNT_SELECT.format(**fmt), filter_args, one=True, cur=cur)["total"]
        )
        rows = run(LOCATIONS_SELECT.format(**fmt), (*filter_args, limit, offset), cur=cur)
        by_position = (
            _candidates_for([r["proposal_position_id"] for r in rows], cur)
            if includeCandidates
            else {}
        )

    return {
        "scope": scope.describe(),
        "stagesUnavailable": STAGES_UNAVAILABLE,
        "total": total,
        "limit": limit,
        "offset": offset,
        "locations": [
            {
                **r,
                "stage": int(r["stage"]),
                "stage_name": STAGE_NAMES[int(r["stage"])],
                "candidates": by_position.get(r["proposal_position_id"], []),
            }
            for r in rows
        ],
    }


@router.get("/locationCandidates")
def location_candidates(
    proposalPositionId: List[str] = Query(
        ...,
        description=(
            "One or more proposal_position_id. Repeat the parameter or send them "
            "comma-separated."
        ),
    ),
):
    """The names proposed for one or more locations — Dashboard 2's comparison table.

    Whether a name fits the location's reservation is NOT decided here. That rule lives in
    ../../portal-frontend-code/Backend/main.py's eligibility_flag(), which is the same code
    the write path enforces; a second copy here would drift from it. This endpoint returns
    the candidate's caste category and gender, and /locations returns the position's
    reservation, which is everything the comparison needs to show the fit.
    """
    ids = []
    for value in proposalPositionId:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise HTTPException(400, f"proposalPositionId must be numeric; got {part!r}")
            ids.append(int(part))
    if not ids:
        raise HTTPException(400, "proposalPositionId is required")
    ids = sorted(set(ids))
    by_position = _candidates_for(ids)
    return {
        "locations": [
            {"proposal_position_id": pid, "candidates": by_position.get(pid, [])}
            for pid in ids
        ]
    }
