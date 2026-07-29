"""Meetings dashboard API (read-only analytics over dakavara_pa party_meeting*).

Prefix added in main.py:
    app.include_router(meetings_router, prefix="/api/v1/meetings", tags=["Meetings"])
"""
import logging
from fastapi import APIRouter, Depends, Query, HTTPException

from app.database.db import dakavara_session
from app.repositories.meetings_repository import MeetingsRepository
from app.schemas.nominated_schema import ApiResponse
from app.services.meetings_service import MeetingsService

logger = logging.getLogger(__name__)
router = APIRouter()


def get_service():
    with dakavara_session() as db:
        yield MeetingsService(MeetingsRepository(db))


def _fail(api_name, exc):
    logger.exception("api=%s status=failed error=%s", api_name, str(exc))
    raise HTTPException(status_code=500, detail={
        "success": False, "error": "INTERNAL_SERVER_ERROR",
        "message": f"{api_name} failed. Please check FastAPI logs."})


def _filters(frm, to, mainType, type_, level, occurrence, conducted, ivr, q=None):
    return {"from": frm, "to": to, "mainType": mainType, "type": type_, "level": level,
            "occurrence": occurrence, "conducted": conducted, "ivr": ivr, "q": q}


@router.get("/filters", response_model=ApiResponse)
def filters(service: MeetingsService = Depends(get_service)):
    try:
        return ApiResponse(data=service.filters())
    except Exception as exc:
        _fail("meetings_filters", exc)


@router.get("/overview", response_model=ApiResponse)
def overview(frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
             mainType: str | None = Query(None), type: str | None = Query(None),
             level: str | None = Query(None), occurrence: str | None = Query(None),
             conducted: str | None = Query(None), ivr: str | None = Query(None),
             service: MeetingsService = Depends(get_service)):
    try:
        return ApiResponse(data=service.overview(
            _filters(frm, to, mainType, type, level, occurrence, conducted, ivr)))
    except Exception as exc:
        _fail("meetings_overview", exc)


@router.get("/list", response_model=ApiResponse)
def meetings(frm: str | None = Query(None, alias="from"), to: str | None = Query(None),
             mainType: str | None = Query(None), type: str | None = Query(None),
             level: str | None = Query(None), occurrence: str | None = Query(None),
             conducted: str | None = Query(None), ivr: str | None = Query(None),
             q: str | None = Query(None), sort: str = Query("recent"),
             limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
             service: MeetingsService = Depends(get_service)):
    try:
        return ApiResponse(data=service.meetings(
            _filters(frm, to, mainType, type, level, occurrence, conducted, ivr, q),
            limit=limit, offset=offset, sort=sort))
    except Exception as exc:
        _fail("meetings_list", exc)
