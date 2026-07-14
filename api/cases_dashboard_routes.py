import logging
from fastapi import APIRouter, Depends, Query, HTTPException

from db_config.db import dakavara_session
from repositories.cases_repository import CasesRepository
from dto.nominated_schema import ApiResponse
from services.cases_dashboard_service import CasesDashboardService

logger = logging.getLogger(__name__)

# Prefix added in main.py: app.include_router(cases_dashboard_router,
#   prefix="/api/v1/cases", tags=["Cases Dashboard"])
router = APIRouter()


def get_service():
    with dakavara_session() as dak_db:
        yield CasesDashboardService(CasesRepository(dak_db))


def _handle_error(api_name: str, exc: Exception):
    logger.exception("api=%s status=failed error=%s", api_name, str(exc))
    raise HTTPException(status_code=500, detail={
        "success": False, "error": "INTERNAL_SERVER_ERROR",
        "message": f"{api_name} failed. Please check FastAPI logs."})


@router.get("/geo", response_model=ApiResponse)
def geo(service: CasesDashboardService = Depends(get_service)):
    try:
        return ApiResponse(data=service.geo_tree())
    except Exception as exc:
        _handle_error("cases_geo", exc)


@router.get("/overview", response_model=ApiResponse)
def overview(parliament: str | None = Query(None), constituency: str | None = Query(None),
             service: CasesDashboardService = Depends(get_service)):
    try:
        return ApiResponse(data=service.overview(parliament, constituency))
    except Exception as exc:
        _handle_error("cases_overview", exc)


@router.get("/leaders", response_model=ApiResponse)
def leaders(parliament: str | None = Query(None), constituency: str | None = Query(None),
            scope: str = Query("leaders", pattern="^(leaders|all)$"),
            limit: int = Query(200, ge=1, le=1000),
            service: CasesDashboardService = Depends(get_service)):
    try:
        return ApiResponse(data=service.leaders(parliament, constituency, scope, limit))
    except Exception as exc:
        _handle_error("cases_leaders", exc)


@router.get("/search", response_model=ApiResponse)
def search(q: str = Query(..., min_length=2), limit: int = Query(50, ge=1, le=200),
           service: CasesDashboardService = Depends(get_service)):
    try:
        return ApiResponse(data=service.search(q, limit))
    except Exception as exc:
        _handle_error("cases_search", exc)


@router.get("/leaders/{leader_key:path}", response_model=ApiResponse)
def leader_detail(leader_key: str, service: CasesDashboardService = Depends(get_service)):
    try:
        data = service.leader_detail(leader_key)
        if data is None:
            raise HTTPException(status_code=404, detail={"success": False, "message": "Leader not found"})
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as exc:
        _handle_error("cases_leader_detail", exc)
