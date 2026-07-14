import logging
from fastapi import APIRouter, Depends, Query

from db_config.db import dakavara_session, pa_track_session
from repositories.nominated_repository import NominatedRepository
from dto.nominated_schema import ApiResponse
from services.nominated_service import NominatedService
from core.config import get_settings

logger = logging.getLogger(__name__)
#router = APIRouter(prefix="/nominated", tags=["Nominated Post Read APIs"])
router = APIRouter()


def get_service():
    with dakavara_session() as dak_db, pa_track_session() as pa_db:
        yield NominatedService(NominatedRepository(dak_db, pa_db))


@router.get("/bootstrap", response_model=ApiResponse)
def nominated_bootstrap(
    enrollmentId: int = Query(default_factory=lambda: get_settings().default_enrollment_id),
    boardLevelId: int = Query(...),
    locationValue: int = Query(...),
    service: NominatedService = Depends(get_service),
):
    logger.info("api=nominated_bootstrap enrollmentId=%s boardLevelId=%s locationValue=%s", enrollmentId, boardLevelId, locationValue)
    return ApiResponse(data=service.bootstrap(enrollmentId, boardLevelId, locationValue))


@router.get("/locations", response_model=ApiResponse)
def locations(boardLevelId: int, stateId: int | None = None, service: NominatedService = Depends(get_service)):
    logger.info("api=locations boardLevelId=%s stateId=%s", boardLevelId, stateId)
    return ApiResponse(data=service.locations(boardLevelId, stateId))


@router.get("/boards", response_model=ApiResponse)
def boards(enrollmentId: int, boardLevelId: int, locationValue: int, departmentId: int, service: NominatedService = Depends(get_service)):
    logger.info("api=boards enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s", enrollmentId, boardLevelId, locationValue, departmentId)
    return ApiResponse(data=service.boards(enrollmentId, boardLevelId, locationValue, departmentId))


@router.get("/positions", response_model=ApiResponse)
def positions(enrollmentId: int, boardLevelId: int, locationValue: int, departmentId: int, boardId: int, service: NominatedService = Depends(get_service)):
    logger.info("api=positions enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s boardId=%s", enrollmentId, boardLevelId, locationValue, departmentId, boardId)
    return ApiResponse(data=service.positions(enrollmentId, boardLevelId, locationValue, departmentId, boardId))


@router.get("/capacity", response_model=ApiResponse)
def capacity(enrollmentId: int, boardLevelId: int, locationValue: int, departmentId: int, boardId: int, positionId: int, service: NominatedService = Depends(get_service)):
    logger.info("api=capacity enrollmentId=%s boardLevelId=%s locationValue=%s departmentId=%s boardId=%s positionId=%s", enrollmentId, boardLevelId, locationValue, departmentId, boardId, positionId)
    return ApiResponse(data=service.capacity(enrollmentId, boardLevelId, locationValue, departmentId, boardId, positionId))


@router.get("/cadre/search", response_model=ApiResponse)
def search_cadre(q: str, limit: int = 10, service: NominatedService = Depends(get_service)):
    logger.info("api=search_cadre query=%s limit=%s", q, limit)
    return ApiResponse(data=service.search_cadre(q, limit))


@router.post("/cache/refresh", response_model=ApiResponse)
def refresh_cache(service: NominatedService = Depends(get_service)):
    logger.info("api=refresh_cache")
    return ApiResponse(data=service.refresh_cache())
