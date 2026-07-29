import logging
from fastapi import APIRouter, Depends, Query, HTTPException

from app.database.db import dakavara_session, pa_track_session
from app.repositories.nominated_repository import NominatedRepository
from app.schemas.nominated_schema import ApiResponse
from app.services.nominated_service import NominatedService
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# IMPORTANT:
# Do not add prefix here. Prefix is already added in main.py:
# app.include_router(nominated_post_router, prefix="/api/v1/nominated-post", tags=["Nominated Post"])
router = APIRouter()


def get_service():
    with dakavara_session() as dak_db, pa_track_session() as pa_db:
        yield NominatedService(NominatedRepository(dak_db, pa_db))


def _handle_error(api_name: str, exc: Exception):
    logger.exception("api=%s status=failed error=%s", api_name, str(exc))
    raise HTTPException(
        status_code=500,
        detail={
            "success": False,
            "error": "INTERNAL_SERVER_ERROR",
            "message": f"{api_name} failed. Please check FastAPI logs.",
        },
    )


@router.get("/bootstrap", response_model=ApiResponse)
def nominated_bootstrap(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    api_name = "nominated_bootstrap"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s",
        api_name, enrollmentId, boardLevelId, locationValue,
    )
    try:
        return ApiResponse(data=service.bootstrap(enrollmentId, boardLevelId, locationValue))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/locations", response_model=ApiResponse)
def locations(
    boardLevelId: int = Query(...),
    stateId: int | None = Query(None),
    service: NominatedService = Depends(get_service),
):
    api_name = "locations"
    logger.info("api=%s boardLevelId=%s stateId=%s", api_name, boardLevelId, stateId)
    try:
        return ApiResponse(data=service.locations(boardLevelId, stateId))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/departments", response_model=ApiResponse)
def departments(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    """
    Returns all departments for the selected level/location with totalSeats,
    filledSeats, and unfilledSeats per department.

    Example:
    /api/v1/nominated-post/departments?enrollmentId=2&boardLevelId=2&locationValue=1
    """
    api_name = "departments"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s",
        api_name, enrollmentId, boardLevelId, locationValue,
    )
    try:
        return ApiResponse(data=service.departments(enrollmentId, boardLevelId, locationValue))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/boards", response_model=ApiResponse)
def boards(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    departmentId: int | None = Query(None),
    service: NominatedService = Depends(get_service),
):
    """
    Returns boards with totalSeats, filledSeats, and unfilledSeats. When departmentId
    is supplied, scopes to that department. When omitted, returns ALL boards for the
    level/location, each carrying its owning departmentId/departmentName.
    """
    api_name = "boards"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s",
        api_name, enrollmentId, boardLevelId, locationValue, departmentId,
    )
    try:
        return ApiResponse(data=service.boards(enrollmentId, boardLevelId, locationValue, departmentId))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/positions", response_model=ApiResponse)
def positions(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    departmentId: int = Query(...),
    boardId: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    """
    Returns all positions for the selected department and board with totalSeats,
    filledSeats, and unfilledSeats, plus nominatedPostMemberId / nominatedPostPositionId.
    """
    api_name = "positions"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s boardId=%s",
        api_name, enrollmentId, boardLevelId, locationValue, departmentId, boardId,
    )
    try:
        return ApiResponse(data=service.positions(enrollmentId, boardLevelId, locationValue, departmentId, boardId))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/capacity", response_model=ApiResponse)
def capacity(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    departmentId: int = Query(...),
    boardId: int = Query(...),
    positionId: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    api_name = "capacity"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s boardId=%s positionId=%s",
        api_name, enrollmentId, boardLevelId, locationValue, departmentId, boardId, positionId,
    )
    try:
        return ApiResponse(data=service.capacity(enrollmentId, boardLevelId, locationValue, departmentId, boardId, positionId))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.get("/cadre/search", response_model=ApiResponse)
def search_cadre(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, ge=1, le=50),
    service: NominatedService = Depends(get_service),
):
    api_name = "search_cadre"
    logger.info("api=%s query=%s limit=%s", api_name, q, limit)
    try:
        return ApiResponse(data=service.search_cadre(q, limit))
    except Exception as exc:
        _handle_error(api_name, exc)


@router.post("/cache/refresh", response_model=ApiResponse)
def refresh_cache(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    api_name = "refresh_cache"
    logger.info(
        "api=%s enrollmentId=%s boardLevelId=%s locationValue=%s",
        api_name, enrollmentId, boardLevelId, locationValue,
    )
    try:
        # If your service.refresh_cache currently has no parameters, update service accordingly.
        # This route is parameterized so cache can be refreshed per selected board-level/location.
        return ApiResponse(data=service.refresh_cache(enrollmentId, boardLevelId, locationValue))
    except TypeError:
        logger.warning("service.refresh_cache does not accept parameters; falling back to no-arg refresh_cache")
        try:
            return ApiResponse(data=service.refresh_cache())
        except Exception as exc:
            _handle_error(api_name, exc)
    except Exception as exc:
        _handle_error(api_name, exc)
