"""SIR DASHBOARD API — read-only voter-verification analytics.

Verified/active-user metrics come from mytdp.booth_voter; per-AC electoral-roll
totals and PC names come from dakavara_pa. Merged by AC name in the service.

Prefix added in main.py:
    app.include_router(sir_dashboard_router, prefix="/api/v1/sir-dashboard", tags=["SIR Dashboard"])
"""
import logging
from fastapi import APIRouter, Depends, Query, HTTPException

from db_config.db import sir_session, dakavara_session
from repositories.sir_dashboard_repository import SirDashboardRepository
from repositories.sir_reference_repository import SirReferenceRepository
from dto.nominated_schema import ApiResponse
from services.sir_dashboard_service import SirDashboardService

logger = logging.getLogger(__name__)
router = APIRouter()


def get_service():
    with sir_session() as sdb, dakavara_session() as ddb:
        yield SirDashboardService(SirDashboardRepository(sdb), SirReferenceRepository(ddb))


def _fail(api_name, exc):
    logger.exception("api=%s status=failed error=%s", api_name, str(exc))
    raise HTTPException(status_code=500, detail={
        "success": False, "error": "INTERNAL_SERVER_ERROR",
        "message": f"{api_name} failed. Please check FastAPI logs."})


@router.get("/overview", response_model=ApiResponse)
def overview(service: SirDashboardService = Depends(get_service)):
    """Overall Status cards (cumulative + today/yesterday), 14-day trend, status split."""
    try:
        return ApiResponse(data=service.overview())
    except Exception as exc:
        _fail("sir_dashboard_overview", exc)


@router.get("/parliament", response_model=ApiResponse)
def parliament(range: str = Query("today"),
               frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
               service: SirDashboardService = Depends(get_service)):
    """Parliament-wise (PC) verification progress for the selected date range."""
    try:
        return ApiResponse(data=service.parliament(range, frm, to))
    except Exception as exc:
        _fail("sir_dashboard_parliament", exc)


@router.get("/assembly", response_model=ApiResponse)
def assembly(range: str = Query("today"),
             frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
             pc: str | None = Query(None),
             service: SirDashboardService = Depends(get_service)):
    """Assembly-wise (AC) verification progress for the selected range; pass pc to drill into one PC."""
    try:
        return ApiResponse(data=service.assembly(range, frm, to, pc))
    except Exception as exc:
        _fail("sir_dashboard_assembly", exc)


# ---- CUBS / D2D (per-voter field collection) ----
@router.get("/cubs/overview", response_model=ApiResponse)
def cubs_overview(range: str = Query("overall"),
                  frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
                  service: SirDashboardService = Depends(get_service)):
    """CUBS collection totals (visited, forms submitted, mobile/caste/party collected),
    status split, and party / caste-category breakdowns (names resolved)."""
    try:
        return ApiResponse(data=service.cubs_overview(range, frm, to))
    except Exception as exc:
        _fail("sir_dashboard_cubs_overview", exc)


@router.get("/cubs/parliament", response_model=ApiResponse)
def cubs_parliament(range: str = Query("today"),
                    frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
                    service: SirDashboardService = Depends(get_service)):
    """Parliament-wise CUBS metrics for the selected range."""
    try:
        return ApiResponse(data=service.cubs_parliament(range, frm, to))
    except Exception as exc:
        _fail("sir_dashboard_cubs_parliament", exc)


@router.get("/cubs/assembly", response_model=ApiResponse)
def cubs_assembly(range: str = Query("today"),
                  frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
                  pc: str | None = Query(None),
                  service: SirDashboardService = Depends(get_service)):
    """Assembly-wise CUBS metrics; pass pc to drill into one parliamentary constituency."""
    try:
        return ApiResponse(data=service.cubs_assembly(range, frm, to, pc))
    except Exception as exc:
        _fail("sir_dashboard_cubs_assembly", exc)
