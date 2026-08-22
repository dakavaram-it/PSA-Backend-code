# Backend/routers/writes.py — the three changes Dashboard 2 can make to a candidate.
#
#   POST /api/dashboard2/confirmCandidate   Proposed -> Confirmed  (stage 1 -> 2)
#   POST /api/dashboard2/markNominated      is_nominated Y/N       (stage 2 -> 3)
#   POST /api/dashboard2/removeCandidate    is_active N            (drops off the location)
#
# All three are guarded by auth.require_user: a valid portal session token, and the acting
# user id taken from that token rather than from the request body.
#
# ---------------------------------------------------------------------------
# THESE RULES EXIST TWICE. KEEP THEM IN STEP.
# ---------------------------------------------------------------------------
# ../../portal-frontend-code/Backend/main.py owns the originals —
# updateProposalCandidateStatus and removeProposalCandidate. Routing Dashboard 2's writes
# through those endpoints was the alternative; putting them here instead was a deliberate
# call, and the cost is that a fix applied there has to be applied here too. Each function
# below names the endpoint it mirrors. If you change one, change the other.
#
# What is deliberately NOT duplicated: proposing a candidate. assignProposalCandidate
# carries the eligibility rules (assembly match, caste category, gender) and the slot and
# Confirmed-completes-the-position checks behind its 409s. None of that is restated here,
# because none of the three writes below creates a candidate row — they only move one that
# assignProposalCandidate already vetted.
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_user
from db import run_write_tx

router = APIRouter(prefix="/api/dashboard2", tags=["dashboard2-writes"])

# proposal_status holds exactly two rows: 1 Proposed, 2 Confirmed. Shortlisted was dropped
# and Confirmed moved down from 3 to 2 with it, so any id 3 anywhere is stale.
PROPOSED_STATUS_ID = 1
CONFIRMED_STATUS_ID = 2


class CandidateRef(BaseModel):
    proposal_candidate_id: int


class ConfirmCandidate(CandidateRef):
    # Confirming is the point of the button, but the same endpoint un-confirms: the compare
    # modal has to be able to undo a mis-click, and a second endpoint for that would be a
    # second copy of the identical UPDATE.
    proposal_status_id: int = CONFIRMED_STATUS_ID


class MarkNominated(CandidateRef):
    is_nominated: str = "Y"


def _live_candidate(cur, proposal_candidate_id: int):
    """The row, or 404. `is_active = 'Y'` throughout: a removed candidate is on no screen,
    and letting one through would edit a row every read here filters out."""
    cur.execute(
        "SELECT proposal_candidate_id, proposal_position_id, proposal_status_id, is_nominated "
        "FROM proposal_candidate WHERE proposal_candidate_id = %s AND is_active = 'Y'",
        (proposal_candidate_id,),
    )
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, "Unknown or removed proposal_candidate_id")
    return rows[0]


@router.post("/confirmCandidate")
def confirm_candidate(body: ConfirmCandidate, user_id: int = Depends(require_user)):
    """Mirrors ../../portal-frontend-code's updateProposalCandidateStatus.

    Touches proposal_status_id alone, so the position, the cadre and every proposed_cnt are
    unaffected — every status is a live row consuming the same max_proposals slot, which is
    why no slot or eligibility re-check belongs here.

    One rule this adds that the original does not need: a position holds at most ONE
    Confirmed candidate. Confirming a second while the first still stands would put the
    location in a state the Dashboard's own "Completed" rule says cannot exist, so it is
    refused with a 409 naming the candidate already holding the seat.
    """
    if body.proposal_status_id not in (PROPOSED_STATUS_ID, CONFIRMED_STATUS_ID):
        raise HTTPException(400, "Unknown proposal_status_id")

    def work(cur):
        row = _live_candidate(cur, body.proposal_candidate_id)
        if body.proposal_status_id == CONFIRMED_STATUS_ID:
            cur.execute(
                "SELECT proposal_candidate_id FROM proposal_candidate "
                "WHERE proposal_position_id = %s AND proposal_status_id = %s "
                "AND is_active = 'Y' AND proposal_candidate_id <> %s",
                (row["proposal_position_id"], CONFIRMED_STATUS_ID, body.proposal_candidate_id),
            )
            held = cur.fetchall()
            if held:
                raise HTTPException(
                    409,
                    "This location already has a confirmed candidate "
                    f"(proposal_candidate_id {held[0]['proposal_candidate_id']}). "
                    "Un-confirm that one first.",
                )
        cur.execute(
            "UPDATE proposal_candidate SET proposal_status_id = %s, updated_time = NOW(), "
            "updated_user_id = %s WHERE proposal_candidate_id = %s AND is_active = 'Y'",
            (body.proposal_status_id, user_id, body.proposal_candidate_id),
        )
        # 0 affected rows means "already that status", not "missing" — _live_candidate has
        # already proved the row exists. Re-saving a status that did not move is not an
        # error the screen should show.
        return {
            "proposal_candidate_id": body.proposal_candidate_id,
            "proposal_status_id": body.proposal_status_id,
        }

    return run_write_tx(work)


@router.post("/markNominated")
def mark_nominated(body: MarkNominated, user_id: int = Depends(require_user)):
    """Move a confirmed candidate to Nomination filed, or back.

    Dashboard 2's stage 3 is proposal_candidate.is_nominated = 'Y' — see queries.STAGE_EXPR.
    It is NOT the nomination PDF: ../../portal-frontend-code's uploadNominationFile writes
    election_candidate_file rows, which this screen's stage ladder does not read at all.
    Uploading a PDF there will not move a location here, and this will not produce a PDF.

    Refused unless the candidate is Confirmed, because the ladder only runs one way: a
    location cannot file papers for a name the location has not settled on.
    """
    flag = body.is_nominated.upper()
    if flag not in ("Y", "N"):
        raise HTTPException(400, "is_nominated must be Y or N")

    def work(cur):
        row = _live_candidate(cur, body.proposal_candidate_id)
        if flag == "Y" and row["proposal_status_id"] != CONFIRMED_STATUS_ID:
            raise HTTPException(409, "Confirm this candidate before filing their nomination")
        cur.execute(
            "UPDATE proposal_candidate SET is_nominated = %s, updated_time = NOW(), "
            "updated_user_id = %s WHERE proposal_candidate_id = %s AND is_active = 'Y'",
            (flag, user_id, body.proposal_candidate_id),
        )
        return {"proposal_candidate_id": body.proposal_candidate_id, "is_nominated": flag}

    return run_write_tx(work)


@router.post("/removeCandidate")
def remove_candidate(body: CandidateRef, user_id: int = Depends(require_user)):
    """Mirrors ../../portal-frontend-code's removeProposalCandidate.

    is_active flips to 'N' rather than the row being deleted — that flag is what every read
    filters on, so the candidate leaves the location and its slot reopens, while who was
    proposed and when survives. updated_user_id is stamped here; the original does not
    record it, which is a gap on that side rather than an extra on this one.
    """

    def work(cur):
        _live_candidate(cur, body.proposal_candidate_id)
        cur.execute(
            "UPDATE proposal_candidate SET is_active = 'N', updated_time = NOW(), "
            "updated_user_id = %s WHERE proposal_candidate_id = %s AND is_active = 'Y'",
            (user_id, body.proposal_candidate_id),
        )
        return {"proposal_candidate_id": body.proposal_candidate_id, "is_active": "N"}

    return run_write_tx(work)
