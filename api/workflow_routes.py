import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from db_config.db import pa_track_session
from repositories.workflow_repository import WorkflowRepository
from services.workflow_service import WorkflowService
from dto.workflow_schema import ApiResponse, WorkflowActionRequest, SaveReviewsRequest, WorkflowFeedbackSaveRequest
from core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

def get_service():
    settings = get_settings()
    enable_legacy_sync = getattr(settings, "enable_legacy_sync", False)
    with pa_track_session() as pa_db:
        yield WorkflowService(WorkflowRepository(pa_db), enable_legacy_sync=enable_legacy_sync)

def handle_error(api_name: str, exc: Exception):
    logger.exception("api=%s failed error=%s", api_name, str(exc))
    status_code = 400 if isinstance(exc, ValueError) else 500
    error_code = "VALIDATION_ERROR" if isinstance(exc, ValueError) else "WORKFLOW_ERROR"
    raise HTTPException(status_code=status_code, detail={"success": False, "error": error_code, "message": str(exc)})

@router.get("/workflow/proposals/{proposalId}/actions", response_model=ApiResponse)
def allowed_actions(proposalId: int = Path(...), roleCode: str = Query("ADMIN"), service: WorkflowService = Depends(get_service)):
    try: return ApiResponse(data=service.actions(proposalId, roleCode))
    except Exception as exc: handle_error("workflow_allowed_actions", exc)

@router.post("/workflow/proposals/{proposalId}/feedbacks", response_model=ApiResponse)
def save_feedbacks_only(
    req: WorkflowFeedbackSaveRequest,
    proposalId: int = Path(...),
    service: WorkflowService = Depends(get_service),
):
    try:
        return ApiResponse(data=service.save_feedbacks_only(
            proposalId, req.feedbacks, req.actionBy, req.actionByName or "",
        ))
    except Exception as exc:
        handle_error("workflow_save_feedbacks", exc)

@router.post("/workflow/proposals/{proposalId}/actions", response_model=ApiResponse)
def execute_action(req: WorkflowActionRequest, proposalId: int = Path(...), service: WorkflowService = Depends(get_service)):
    try: return ApiResponse(data=service.execute_action(proposalId, req))
    except Exception as exc: handle_error("workflow_execute_action", exc)

@router.put("/workflow/proposals/{proposalId}/reviews", response_model=ApiResponse)
def save_reviews(req: SaveReviewsRequest, proposalId: int = Path(...), service: WorkflowService = Depends(get_service)):
    try: return ApiResponse(data=service.update_reviews(proposalId, req))
    except Exception as exc: handle_error("workflow_save_reviews", exc)

@router.get("/workflow/proposals/{proposalId}/history", response_model=ApiResponse)
def workflow_history(proposalId: int = Path(...), service: WorkflowService = Depends(get_service)):
    try: return ApiResponse(data=service.history(proposalId))
    except Exception as exc: handle_error("workflow_history", exc)
