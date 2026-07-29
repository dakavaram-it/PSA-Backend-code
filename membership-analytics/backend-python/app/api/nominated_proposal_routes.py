import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Path, Body
from sqlalchemy.exc import IntegrityError
from app.database.db import dakavara_session, pa_track_session, report_ratings_session, update_session, mytdp_session
from app.repositories.nominated_proposal_repository import NominatedProposalRepository
from app.repositories.cadre_profile_repository import CadreProfileRepository
from app.repositories.cadre_app_usage_repository import CadreAppUsageRepository
from app.repositories.cadre_performance_repository import CadrePerformanceRepository
from app.services.nominated_proposal_service import NominatedProposalService
from app.services.cadre_performance_service import CadrePerformanceService
from app.schemas.nominated_proposal_schema import (
    ApiResponse,
    ProposalCreateRequest,
    ProposalCandidateAddRequest,
    ManualCandidateCreateRequest,
    ProposalStatusUpdateRequest,
    CompareCandidatesRequest,
    CasteUpdateRequest,
    OccupationUpdateRequest,
    EducationUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

def get_service():
    with dakavara_session() as dak_db, pa_track_session() as pa_db, update_session() as write_db:
        repo = NominatedProposalRepository(dak_db, pa_db)
        profile_repo = CadreProfileRepository(dak_db, write_db=write_db)
        cadre_performance = CadrePerformanceService(profile_repo, None)
        if CadrePerformanceService.is_configured():
            with report_ratings_session() as rr_db:
                cadre_performance = CadrePerformanceService(
                    profile_repo, CadrePerformanceRepository(rr_db)
                )
                yield NominatedProposalService(repo, cadre_performance)
        else:
            yield NominatedProposalService(repo, cadre_performance)

def handle_error(api_name: str, exc: Exception):
    logger.exception("api=%s failed error=%s", api_name, str(exc))
    if isinstance(exc, ValueError):
        status_code, error_code = 400, "VALIDATION_ERROR"
    elif isinstance(exc, IntegrityError):
        status_code, error_code = 409, "DUPLICATE_CANDIDATE"
        exc = ValueError("One or more candidates are already on this proposal")
    else:
        status_code, error_code = 500, "API_ERROR"
    raise HTTPException(status_code=status_code, detail={"success": False, "error": error_code, "message": str(exc)})

@router.get("/proposals", response_model=ApiResponse)
def list_proposals(
    enrollmentId: int = Query(2, ge=1),
    statusCode: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.list_proposals(
            enrollment_id=enrollmentId,
            limit=limit,
            offset=offset,
            status_code=statusCode,
        ))
    except Exception as exc:
        handle_error("list_nominated_post_proposals", exc)

@router.get("/proposals/candidates/search", response_model=ApiResponse)
def search_candidates(
    mid: str | None = Query(None),
    mobile: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    service: NominatedProposalService = Depends(get_service),
):
    """
    Search cadre by MID or mobile.

    For each resolved MID, run ``cadre_performance_update`` → ``cadre_performance_report``
    first, then read the refreshed ``cadre_details`` in report_ratings and return the
    merged ``profile`` (photoUrl, score, renewals, etc.).
    """
    try:
        return ApiResponse(data=service.search_candidates(mid=mid, mobile=mobile, limit=limit))
    except Exception as exc:
        handle_error("search_candidates_for_nominated_post_proposal", exc)

@router.get("/proposals/candidates/pool", response_model=ApiResponse)
def list_candidate_pool(
    limit: int = Query(500, ge=1, le=50000),
    service: NominatedProposalService = Depends(get_service),
):
    """Read-only candidate pool from report_ratings.cadre_details, ranked by the
    existing PERFORMANCE SCORE. Registered before /{proposalId} so 'candidates'
    isn't parsed as a proposal id."""
    try:
        return ApiResponse(data=service.list_candidate_pool(limit=limit))
    except Exception as exc:
        handle_error("list_candidate_pool", exc)

@router.post("/proposals/candidates/pool/compare", response_model=ApiResponse)
def compare_pool_candidates(
    req: CompareCandidatesRequest | None = Body(default=None),
    service: NominatedProposalService = Depends(get_service),
):
    """Compare selected Candidate Pool cadres. Reads only the nominated-post pool
    snapshot + feedback tables (cadre_details_nom, cadre_performace_report_nom,
    leader_feedback) — no performance procedures run. Same payload shape as the
    proposal compare so the UI reuses the same CompareModal. Registered before
    /{proposalId} so 'candidates' isn't parsed as a proposal id."""
    try:
        mids = req.mids if req else None
        return ApiResponse(data=service.compare_pool_candidates(mids=mids))
    except Exception as exc:
        handle_error("compare_pool_candidates", exc)

@router.get("/proposals/candidates/profile", response_model=ApiResponse)
def get_candidate_profile_report(
    mid: str = Query(..., min_length=1),
    refresh: bool = Query(False, description="Run performance update/report before returning data"),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.get_candidate_profile_report(mid, refresh=refresh))
    except Exception as exc:
        handle_error("get_candidate_profile_report", exc)

@router.get("/proposals/candidates/app-usage", response_model=ApiResponse)
def get_candidate_app_usage(mid: str = Query(..., min_length=1)):
    """MY TDP APP USAGE for one membership (points + state/constituency rank +
    feed post/event counts) from the mytdp app DB. Registered before
    ``/proposals/{proposalId}`` so ``candidates`` is not parsed as a proposal id."""
    try:
        with mytdp_session() as mytdp_db:
            repo = CadreAppUsageRepository(mytdp_db)
            return ApiResponse(data=repo.get_app_usage_by_mid(mid))
    except Exception as exc:
        handle_error("get_candidate_app_usage", exc)

@router.post("/proposals/{proposalId}/candidates/compare", response_model=ApiResponse)
def compare_proposal_candidates(
    proposalId: int = Path(...),
    req: CompareCandidatesRequest | None = Body(default=None),
    service: NominatedProposalService = Depends(get_service),
):
    """
    Compare candidates on a proposal.

    For each MID, run ``cadre_performance_update`` → ``cadre_performance_report`` first,
    then read the refreshed ``cadre_details`` and ``cadre_performace_report`` rows.

    Returns the full merged payload per candidate (dakavara profile, cadre_details,
    cadre_performance_report, profile, performance, photoUrl).

    - Send **no body** or `{}` to compare all saved candidates on the proposal.
    - Or send `{"mids": ["15067518", "36967181"]}` to compare specific MIDs.
    - Invalid placeholders (e.g. Swagger default `"string"`) are ignored; proposal MIDs are used instead.
    """
    try:
        mids = req.mids if req else None
        return ApiResponse(data=service.compare_proposal_candidates(proposalId, mids=mids))
    except Exception as exc:
        handle_error("compare_nominated_post_proposal_candidates", exc)

@router.get("/caste-options", response_model=ApiResponse)
def get_caste_options(
    stateId: int = Query(1, ge=1),
    service: NominatedProposalService = Depends(get_service),
):
    """Caste category groups + castes (with caste_state_id) for a state, for the caste edit dropdowns."""
    try:
        return ApiResponse(data=service.get_caste_options(stateId))
    except Exception as exc:
        handle_error("get_caste_options", exc)

@router.patch("/proposals/candidates/caste", response_model=ApiResponse)
def update_candidate_caste(
    req: CasteUpdateRequest,
    service: NominatedProposalService = Depends(get_service),
):
    """Persist a candidate's caste (tdp_cadre.caste_state_id by membership_id), re-run the
    performance procedures, and return the refreshed profile."""
    try:
        return ApiResponse(data=service.update_candidate_caste(req.mid, req.casteStateId))
    except Exception as exc:
        handle_error("update_candidate_caste", exc)

@router.get("/occupation-options", response_model=ApiResponse)
def get_occupation_options(
    service: NominatedProposalService = Depends(get_service),
):
    """All occupations (`occupationId`/`occupation`) for the occupation edit dropdown."""
    try:
        return ApiResponse(data=service.get_occupation_options())
    except Exception as exc:
        handle_error("get_occupation_options", exc)

@router.patch("/proposals/candidates/occupation", response_model=ApiResponse)
def update_candidate_occupation(
    req: OccupationUpdateRequest,
    service: NominatedProposalService = Depends(get_service),
):
    """Persist a candidate's occupation (tdp_cadre.occupation_id by membership_id), re-run the
    performance procedures, and return the refreshed profile."""
    try:
        return ApiResponse(data=service.update_candidate_occupation(req.mid, req.occupationId))
    except Exception as exc:
        handle_error("update_candidate_occupation", exc)

@router.get("/education-options", response_model=ApiResponse)
def get_education_options(
    service: NominatedProposalService = Depends(get_service),
):
    """All educational qualifications (`educationId`/`education`) for the education edit dropdown."""
    try:
        return ApiResponse(data=service.get_education_options())
    except Exception as exc:
        handle_error("get_education_options", exc)

@router.get("/party-options", response_model=ApiResponse)
def get_party_options(
    service: NominatedProposalService = Depends(get_service),
):
    """Parties for the manual (no-MID) candidate create dropdown."""
    try:
        return ApiResponse(data=service.get_party_options())
    except Exception as exc:
        handle_error("get_party_options", exc)

@router.patch("/proposals/candidates/education", response_model=ApiResponse)
def update_candidate_education(
    req: EducationUpdateRequest,
    service: NominatedProposalService = Depends(get_service),
):
    """Persist a candidate's education (tdp_cadre.education_id by membership_id), re-run the
    performance procedures, and return the refreshed profile."""
    try:
        return ApiResponse(data=service.update_candidate_education(req.mid, req.educationId))
    except Exception as exc:
        handle_error("update_candidate_education", exc)

@router.post("/proposals", response_model=ApiResponse)
def create_proposal(req: ProposalCreateRequest, service: NominatedProposalService = Depends(get_service)):
    try:
        return ApiResponse(data=service.create_proposal(req))
    except Exception as exc:
        handle_error("create_nominated_post_proposal", exc)

@router.get("/proposals/{proposalId}", response_model=ApiResponse)
def get_proposal(proposalId: int = Path(...), service: NominatedProposalService = Depends(get_service)):
    """
    Get proposal detail. Each item in ``candidates`` includes a merged ``profile``
    (same shape as compare/search): photoUrl, performanceScore, renewalTimes, etc.
    """
    try:
        return ApiResponse(data=service.get_proposal(proposalId))
    except Exception as exc:
        handle_error("get_nominated_post_proposal", exc)

@router.post("/proposals/{proposalId}/candidates", response_model=ApiResponse)
def add_candidates(req: ProposalCandidateAddRequest, proposalId: int = Path(...), service: NominatedProposalService = Depends(get_service)):
    try:
        return ApiResponse(data=service.add_candidates(proposalId, req))
    except Exception as exc:
        handle_error("add_nominated_post_proposal_candidates", exc)

@router.post("/proposals/{proposalId}/candidates/manual", response_model=ApiResponse)
def add_manual_candidate(req: ManualCandidateCreateRequest, proposalId: int = Path(...), service: NominatedProposalService = Depends(get_service)):
    """Create a brand-new candidate (not in tdp_cadre) directly on the proposal."""
    try:
        return ApiResponse(data=service.add_manual_candidate(proposalId, req))
    except Exception as exc:
        handle_error("add_manual_nominated_post_proposal_candidate", exc)

@router.delete("/proposals/{proposalId}/candidates", response_model=ApiResponse)
def remove_all_candidates(
    proposalId: int = Path(...),
    actionBy: int = Query(0),
    actionByName: str | None = Query(None),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.remove_all_candidates(
            proposalId, actionBy, actionByName or "",
        ))
    except Exception as exc:
        handle_error("remove_all_nominated_post_proposal_candidates", exc)

@router.delete("/proposals/{proposalId}/candidates/{proposalCandidateId}", response_model=ApiResponse)
def remove_candidate(
    proposalId: int = Path(...),
    proposalCandidateId: int = Path(...),
    actionBy: int = Query(0),
    actionByName: str | None = Query(None),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.remove_candidate(
            proposalId, proposalCandidateId, actionBy, actionByName or "",
        ))
    except Exception as exc:
        handle_error("remove_nominated_post_proposal_candidate", exc)

@router.delete("/proposals/{proposalId}/delete", response_model=ApiResponse)
def delete_proposal(
    proposalId: int = Path(...),
    actionBy: int = Query(0, ge=0),
    actionByName: str = Query(""),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.delete_proposal(proposalId, actionBy, actionByName))
    except Exception as exc:
        handle_error("delete_nominated_post_proposal", exc)

@router.post("/proposals/{proposalId}/revert-stage", response_model=ApiResponse)
def revert_proposal_stage(
    proposalId: int = Path(...),
    actionBy: int = Query(0, ge=0),
    actionByName: str = Query(""),
    service: NominatedProposalService = Depends(get_service),
):
    """Move the proposal one workflow stage back (detail "Back" button)."""
    try:
        return ApiResponse(data=service.revert_to_previous_stage(proposalId, actionBy, actionByName))
    except Exception as exc:
        handle_error("revert_nominated_post_proposal_stage", exc)

@router.patch("/proposals/{proposalId}/status", response_model=ApiResponse)
def update_proposal_status(
    req: ProposalStatusUpdateRequest,
    proposalId: int = Path(...),
    service: NominatedProposalService = Depends(get_service),
):
    try:
        remarks = req.remarks
        if not remarks and req.goNumber:
            remarks = f"GO {req.goNumber}" + (f" · {req.goDate}" if req.goDate else "")
        return ApiResponse(
            data=service.update_proposal_status(
                proposalId,
                req.statusCode,
                remarks=remarks,
                action_by=req.actionBy,
                action_by_name=req.actionByName or "",
            )
        )
    except Exception as exc:
        handle_error("update_nominated_post_proposal_status", exc)
