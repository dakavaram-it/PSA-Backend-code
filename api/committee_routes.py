import logging
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Body
from sqlalchemy.exc import IntegrityError
from db_config.db import (
    pa_track_session,
    dakavara_session as dakavara_pa_session,
    report_ratings_session,
)
from repositories.committee_repository import CommitteeRepository
from repositories.cadre_profile_repository import CadreProfileRepository
from repositories.cadre_performance_repository import CadrePerformanceRepository
from services.committee_service import CommitteeService
from services.cadre_performance_service import CadrePerformanceService
from dto.committee_schema import (
    ApiResponse,
    CommitteeProposalCreateRequest,
    CommitteeMembersAddRequest,
    CommitteeWorkflowActionRequest,
    CommitteeFeedbackSaveRequest,
    CommitteeCompareRequest,
    CommitteeStatusUpdateRequest,
)
from core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def get_service():
    settings = get_settings()
    enable_sync = getattr(settings, "enable_committee_legacy_sync", False)
    enable_kss = getattr(settings, "enable_committee_kss_check", True)

    with pa_track_session() as pa_db:
        with dakavara_pa_session() as legacy_db:
            yield CommitteeService(
                CommitteeRepository(pa_db, legacy_db),
                enable_committee_legacy_sync=enable_sync,
                enable_committee_kss_check=enable_kss,
            )


def get_cadre_performance():
    """Optional report_ratings enrichment service for candidate compare."""
    with dakavara_pa_session() as dak_db:
        profile_repo = CadreProfileRepository(dak_db)
        if CadrePerformanceService.is_configured():
            with report_ratings_session() as rr_db:
                yield CadrePerformanceService(profile_repo, CadrePerformanceRepository(rr_db))
        else:
            yield CadrePerformanceService(profile_repo, None)


def handle_error(api_name: str, exc: Exception):
    # An HTTPException raised deeper in the stack (e.g. a 404 from a service)
    # must pass through unchanged instead of being re-wrapped as a 500.
    if isinstance(exc, HTTPException):
        raise exc

    logger.exception("api=%s failed error=%s", api_name, str(exc))

    if isinstance(exc, ValueError):
        # Business-rule rejections (duplicate proposal, invalid stage, missing
        # config, etc.) are client errors, not server faults.
        status_code, error_code = 400, "VALIDATION_ERROR"
    elif isinstance(exc, IntegrityError):
        status_code, error_code = 409, "DUPLICATE"
        exc = ValueError("This record conflicts with an existing one (duplicate).")
    else:
        status_code, error_code = 500, "COMMITTEE_ERROR"

    raise HTTPException(
        status_code=status_code,
        detail={"success": False, "error": error_code, "message": str(exc)}
    )


@router.get("/basic-committees", response_model=ApiResponse)
def committee_basic_committees(committeeTypeId: int = Query(...), service: CommitteeService = Depends(get_service)):
    """Step 2 — basic committees for the selected type (1=Main, 2=Affiliated)."""
    try:
        return ApiResponse(data=service.basic_committees(committeeTypeId))
    except Exception as exc:
        handle_error("committee_basic_committees", exc)


@router.get("/committees", response_model=ApiResponse)
def committee_committees(
    levelId: int = Query(...),
    enrollmentId: int = Query(4),
    levelValue: int | None = Query(None),
    tdpBasicCommitteeId: int | None = Query(None),
    committeeTypeId: int | None = Query(None),
    service: CommitteeService = Depends(get_service),
):
    """Step 3 — committees that exist for a level/location/enrollment.

    Optionally filter by committeeTypeId (1=Main, 2=Affiliated) or a single
    tdpBasicCommitteeId. Returns only committees that exist, so the UI can render
    them as selectable cards (like Nominated Post departments).
    """
    try:
        return ApiResponse(data=service.committees(
            levelId, enrollmentId, levelValue, tdpBasicCommitteeId, committeeTypeId
        ))
    except Exception as exc:
        handle_error("committee_committees", exc)


@router.get("/roles", response_model=ApiResponse)
def committee_roles(tdpCommitteeId: int = Query(...), service: CommitteeService = Depends(get_service)):
    """Step 4 & 5 — roles with totalSeats / filledSeats / unfilledSeats for the committee."""
    try:
        return ApiResponse(data=service.committee_roles(tdpCommitteeId))
    except Exception as exc:
        handle_error("committee_roles", exc)


@router.get("/kss-check", response_model=ApiResponse)
def committee_kss_check(mid: str = Query(...), service: CommitteeService = Depends(get_service)):
    """Gate for adding committee candidates: is this membership id part of KSS
    (an active committee membership at tdp_committee_level_id = 18)?"""
    try:
        return ApiResponse(data=service.check_kss(mid))
    except Exception as exc:
        handle_error("committee_kss_check", exc)


@router.post("/proposals", response_model=ApiResponse)
def create_committee_proposal(req: CommitteeProposalCreateRequest, service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.create_proposal(req))
    except Exception as exc:
        handle_error("committee_create_proposal", exc)


@router.post("/proposals/{proposalId}/members", response_model=ApiResponse)
def add_committee_members(req: CommitteeMembersAddRequest, proposalId: int = Path(...), service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.add_members(proposalId, req))
    except Exception as exc:
        handle_error("committee_add_members", exc)


@router.delete("/proposals/{proposalId}/members/{memberId}", response_model=ApiResponse)
def remove_committee_member(
    proposalId: int = Path(...),
    memberId: int = Path(...),
    actionBy: int = Query(0),
    actionByName: str | None = Query(None),
    service: CommitteeService = Depends(get_service),
):
    """Soft-delete a single committee member (parity with Nominated Post candidate removal)."""
    try:
        return ApiResponse(data=service.remove_member(proposalId, memberId, actionBy, actionByName))
    except Exception as exc:
        handle_error("committee_remove_member", exc)


@router.delete("/proposals/{proposalId}/members", response_model=ApiResponse)
def remove_all_committee_members(
    proposalId: int = Path(...),
    actionBy: int = Query(0),
    actionByName: str | None = Query(None),
    service: CommitteeService = Depends(get_service),
):
    """Soft-delete all committee members (used by Fresh Cycle)."""
    try:
        return ApiResponse(data=service.remove_all_members(proposalId, actionBy, actionByName))
    except Exception as exc:
        handle_error("committee_remove_all_members", exc)


@router.delete("/proposals/{proposalId}/delete", response_model=ApiResponse)
def delete_committee_proposal(
    proposalId: int = Path(...),
    actionBy: int = Query(0, ge=0),
    actionByName: str = Query(""),
    service: CommitteeService = Depends(get_service),
):
    """Hard-delete a draft (ADD_PROFILES) proposal; soft-delete it past that stage."""
    try:
        return ApiResponse(data=service.delete_proposal(proposalId, actionBy, actionByName))
    except Exception as exc:
        handle_error("committee_delete_proposal", exc)


@router.patch("/proposals/{proposalId}/status", response_model=ApiResponse)
def update_committee_status(
    req: CommitteeStatusUpdateRequest,
    proposalId: int = Path(...),
    service: CommitteeService = Depends(get_service),
):
    """Direct status override — used by Fresh Cycle to reset to ADD_PROFILES."""
    try:
        return ApiResponse(data=service.update_status(proposalId, req))
    except Exception as exc:
        handle_error("committee_update_status", exc)


@router.post("/proposals/{proposalId}/candidates/compare", response_model=ApiResponse)
def compare_committee_candidates(
    proposalId: int = Path(...),
    req: CommitteeCompareRequest | None = Body(default=None),
    service: CommitteeService = Depends(get_service),
    cadre_performance: CadrePerformanceService = Depends(get_cadre_performance),
):
    """Compare committee candidates by MID (profile + performance enrichment).
    Send no body / `{}` to compare all members, or `{"mids": [...]}` for specific ones."""
    try:
        mids = req.mids if req else None
        return ApiResponse(data=service.compare_candidates(proposalId, mids, cadre_performance))
    except Exception as exc:
        handle_error("committee_compare_candidates", exc)


@router.get("/proposals", response_model=ApiResponse)
def list_committee_proposals(
    limit: int = Query(200, ge=1, le=1000),
    statusCode: str | None = Query(None),
    service: CommitteeService = Depends(get_service),
):
    """List committee proposals (newest first) with positions + members, so the UI
    can rebuild its committee list after a page refresh."""
    try:
        return ApiResponse(data=service.list_proposals(limit, statusCode))
    except Exception as exc:
        handle_error("committee_list_proposals", exc)


@router.get("/proposals/{proposalId}", response_model=ApiResponse)
def get_committee_proposal(
    proposalId: int = Path(...),
    service: CommitteeService = Depends(get_service),
    cadre_performance: CadrePerformanceService = Depends(get_cadre_performance),
):
    try:
        return ApiResponse(data=service.get_proposal(proposalId, cadre_performance))
    except Exception as exc:
        handle_error("committee_get_proposal", exc)


@router.get("/workflow/proposals/{proposalId}/actions", response_model=ApiResponse)
def committee_allowed_actions(proposalId: int = Path(...), roleCode: str = Query("ADMIN"), service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.actions(proposalId, roleCode))
    except Exception as exc:
        handle_error("committee_allowed_actions", exc)


@router.post("/workflow/proposals/{proposalId}/actions", response_model=ApiResponse)
def committee_execute_action(req: CommitteeWorkflowActionRequest, proposalId: int = Path(...), service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.execute_action(proposalId, req))
    except Exception as exc:
        handle_error("committee_execute_action", exc)


@router.post("/workflow/proposals/{proposalId}/feedbacks", response_model=ApiResponse)
def committee_save_feedbacks(req: CommitteeFeedbackSaveRequest, proposalId: int = Path(...), service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.save_feedbacks_only(
            proposalId, req.feedbacks, req.actionBy, req.actionByName or "",
        ))
    except Exception as exc:
        handle_error("committee_save_feedbacks", exc)


@router.get("/workflow/proposals/{proposalId}/history", response_model=ApiResponse)
def committee_history(proposalId: int = Path(...), service: CommitteeService = Depends(get_service)):
    try:
        return ApiResponse(data=service.history(proposalId))
    except Exception as exc:
        handle_error("committee_history", exc)
