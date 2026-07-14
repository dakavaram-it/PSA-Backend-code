import logging
import time
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from db_config.db import dakavara_session
from repositories.pulse_repository import PulseRepository
from repositories.pulse_compute_repository import PulseComputeRepository
from dto.nominated_schema import ApiResponse
from services import pulse_cache, pulse_config as cfg, pulse_panel
from services.pulse_service import (
    PulseService, build_snapshot, constituency_view, invalidate_constituency_views,
    preload_constituency_views,
)

logger = logging.getLogger(__name__)

# Prefix added in main.py: prefix="/api/v1/pulse", tags=["Pulse Trend"]
router = APIRouter()


def get_service():
    with dakavara_session() as dak_db:
        yield PulseService(PulseRepository(dak_db))


@router.get("/overview", response_model=ApiResponse)
def overview(service: PulseService = Depends(get_service)):
    try:
        return ApiResponse(data=service.overview())
    except Exception as exc:
        logger.exception("api=pulse_overview error=%s", str(exc))
        raise HTTPException(status_code=500, detail={
            "success": False, "error": "INTERNAL_SERVER_ERROR",
            "message": "pulse_overview failed. Please check FastAPI logs."})


@router.get("/ivrs", response_model=ApiResponse)
def ivrs(service: PulseService = Depends(get_service)):
    try:
        return ApiResponse(data=service.ivrs())
    except Exception as exc:
        logger.exception("api=pulse_ivrs error=%s", str(exc))
        raise HTTPException(status_code=500, detail={
            "success": False, "error": "INTERNAL_SERVER_ERROR",
            "message": "pulse_ivrs failed. Please check FastAPI logs."})


@router.get("/constituency/{name}", response_model=ApiResponse)
def constituency(name: str):
    """Booth-level drill-down for one constituency. Heavy on first hit (~tens of
    seconds) — then materialized into pulse_trend_* tables, instant after."""
    try:
        with dakavara_session() as dak_db:
            data = constituency_view(PulseComputeRepository(dak_db), name)
        return ApiResponse(data=data)
    except Exception as exc:
        logger.exception("api=pulse_constituency name=%s error=%s", name, str(exc))
        raise HTTPException(status_code=500, detail={
            "success": False, "error": "INTERNAL_SERVER_ERROR",
            "message": "pulse_constituency failed. Please check FastAPI logs."})


def _run_refresh(force=False):
    """Recompute everything from the DB and materialize it into the pulse_trend_*
    tables. Runs prechecks first and aborts on a failed critical check unless forced.
    Heavy (~minutes) — invoked as a background task."""
    st = pulse_cache.STATUS
    st.update(state="running", startedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
              finishedAt=None, error=None, durationSec=None, checks=None)
    t0 = time.time()
    try:
        with dakavara_session() as dak_db:
            repo = PulseComputeRepository(dak_db)
            checks = repo.precheck()
            st["checks"] = checks
            failed = [c for c in checks if c["critical"] and not c["ok"]]
            if failed and not force:
                st.update(state="error", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                          durationSec=round(time.time() - t0, 1),
                          error="Precheck failed: " + ", ".join(c["name"] for c in failed))
                logger.warning("api=pulse_refresh aborted, prechecks failed: %s", failed)
                return
            # state-wide snapshot (IVRS waves, CATI, decline, panel, cadre).
            # Per-constituency booth/segment/houses are materialized lazily on first
            # open (full up-front precompute is too heavy on the shared DB).
            pulse_cache.save(build_snapshot(repo))
        invalidate_constituency_views()
        st.update(state="done", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                  durationSec=round(time.time() - t0, 1))
        logger.info("api=pulse_refresh done in %.1fs", time.time() - t0)
    except Exception as exc:
        st.update(state="error", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                  durationSec=round(time.time() - t0, 1), error=str(exc))
        logger.exception("api=pulse_refresh error=%s", str(exc))


def _check_token(token):
    if cfg.REFRESH_TOKEN and token != cfg.REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail={
            "success": False, "error": "UNAUTHORIZED", "message": "Invalid refresh token."})


# progress for the "materialize every constituency" job (kept separate from STATUS)
WARM_STATUS = {"state": "idle", "done": 0, "total": 0, "current": None,
               "errors": 0, "startedAt": None, "finishedAt": None}


def _warm_all_constituencies(force=False):
    """Materialize every constituency's drill-down into the pulse_trend_* tables,
    one seat at a time (each a short, isolated query+write on its own connection —
    safe for hours, resumable). After this, every seat opens instantly."""
    st = WARM_STATUS
    try:
        with dakavara_session() as db:
            names = PulseComputeRepository(db).list_constituencies()
    except Exception as exc:
        st.update(state="error", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"), current=str(exc)[:120])
        logger.exception("api=pulse_warm list error=%s", str(exc))
        return
    st.update(state="running", total=len(names), done=0, errors=0, current=None,
              startedAt=time.strftime("%Y-%m-%d %H:%M:%S"), finishedAt=None)
    for name in names:
        st["current"] = name
        try:
            if force or pulse_cache.read_constituency(name) is None:
                with dakavara_session() as db:
                    detail = PulseComputeRepository(db).constituency_detail(name)
                pulse_cache.write_constituency(name, detail["dims"], detail["houses"])
        except Exception as exc:
            st["errors"] += 1
            logger.warning("api=pulse_warm seat=%s error=%s", name, str(exc)[:160])
        st["done"] += 1
    st.update(state="done", current=None, finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("api=pulse_warm done: %d seats, %d errors", st["done"], st["errors"])


@router.post("/constituencies/warm", response_model=ApiResponse)
def warm_constituencies(background: BackgroundTasks, force: bool = False,
                        x_refresh_token: str = Header(default=None)):
    """Kick off materialization of ALL constituencies in the background. Long-running
    (~minutes per dozen seats); best off-peak. Poll /constituencies/warm/status."""
    _check_token(x_refresh_token)
    if WARM_STATUS["state"] == "running":
        return ApiResponse(data=WARM_STATUS, message="Warm-up already running.")
    background.add_task(_warm_all_constituencies, force)
    return ApiResponse(data={"state": "running", "force": force}, message="Constituency warm-up started.")


@router.get("/constituencies/warm/status", response_model=ApiResponse)
def warm_status():
    return ApiResponse(data=WARM_STATUS)


# per-round keys kept in the lean /survey payload (full demographic blocks are
# fetched per-AC via /survey/ac so the list payload stays ~60KB, not ~2.5MB).
_LEAN_ROUND_KEYS = ("nda", "ysrcp", "others", "margin", "winner", "samples")


def _lean_snapshot(snap):
    """Strip the heavy per-AC demographic blocks for the list payload."""
    if not snap:
        return snap
    lean = dict(snap)
    lean["perAC"] = [
        {"ac": a["ac"], "rounds": {l: {k: r.get(k) for k in _LEAN_ROUND_KEYS}
                                   for l, r in a.get("rounds", {}).items()}}
        for a in snap.get("perAC", [])
    ]
    return lean


_SURVEY_SNAP = {}  # in-process cache: {agencyId: heavy snapshot blob}


def _survey_snapshot(agency=None):
    """Load (and memoize) one agency's snapshot — from DB, else build+save on demand."""
    from services import pulse_survey
    agency = agency or pulse_survey.DEFAULT_AGENCY
    if agency not in _SURVEY_SNAP:
        snap = pulse_cache.load_named(f"survey:{agency}")
        if snap is None and agency == "codemo":
            snap = pulse_cache.load_named("survey")  # legacy single-agency key
        if snap is None:
            prof = pulse_survey.get_profile(agency)
            if not prof:
                return None
            with dakavara_session() as dak_db:
                snap = pulse_cache.save_named(f"survey:{agency}", pulse_survey.build(dak_db, prof))
        _SURVEY_SNAP[agency] = snap
    return _SURVEY_SNAP[agency]


@router.get("/survey", response_model=ApiResponse)
def survey_intelligence(agency: str = ""):
    """Survey Intelligence snapshot for an agency (lean — per-AC vote/margin only)."""
    snap = _survey_snapshot(agency or None)
    if snap is None:
        raise HTTPException(status_code=404, detail={"success": False, "message": f"Unknown agency {agency}"})
    return ApiResponse(data=_lean_snapshot(snap))


@router.get("/survey/compare", response_model=ApiResponse)
def survey_compare():
    """Cross-agency comparison: statewide blocks + per-round seat split for every loaded
    agency, assembled from the cached snapshots (no DB recompute). Powers State Breakdown
    and the Winners grid."""
    from services import pulse_survey
    agencies = pulse_survey.agency_meta()
    rounds, by_agency = [], {}
    for a in agencies:
        if not a["loaded"]:
            continue
        snap = _survey_snapshot(a["id"])
        if not snap:
            continue
        if not rounds:
            rounds = snap.get("rounds", [])
        seats = {}
        for lbl in snap.get("rounds", []):
            n = y = 0
            for ac in snap.get("perAC", []):
                w = ac.get("rounds", {}).get(lbl, {}).get("winner")
                if w == "NDA":
                    n += 1
                elif w == "YSRCP":
                    y += 1
            if n + y:
                seats[lbl] = {"nda": n, "ysrcp": y, "total": n + y}
        by_agency[a["id"]] = {"name": a["name"], "perRound": snap.get("perRound", []), "seats": seats}
    return ApiResponse(data={"agencies": agencies, "rounds": rounds, "byAgency": by_agency})


@router.get("/survey/ac", response_model=ApiResponse)
def survey_ac_report(name: str = "", agency: str = ""):
    """Full materialized survey blocks for one constituency (AC Report) — instant snapshot lookup."""
    key = (name or "").strip().upper()
    if not key:
        raise HTTPException(status_code=400, detail={"success": False, "message": "name required"})
    snap = _survey_snapshot(agency or None) or {}
    entry = next((a for a in snap.get("perAC", []) if a["ac"].strip().upper() == key), None)
    if entry is None:
        raise HTTPException(status_code=404, detail={"success": False, "message": f"No survey data for {name}"})
    return ApiResponse(data={"entry": entry, "rounds": snap.get("rounds", [])})


@router.post("/survey/refresh", response_model=ApiResponse)
def survey_refresh(background: BackgroundTasks, x_refresh_token: str = Header(default=None)):
    _check_token(x_refresh_token)

    def _run():
        from services import pulse_survey
        try:
            with dakavara_session() as dak_db:
                snaps = pulse_survey.build_all(dak_db)
            for aid, snap in snaps.items():
                pulse_cache.save_named(f"survey:{aid}", snap)
                _SURVEY_SNAP[aid] = snap
            logger.info("api=survey_refresh done agencies=%s", list(snaps))
        except Exception as exc:  # noqa: BLE001
            logger.exception("api=survey_refresh error=%s", str(exc))

    background.add_task(_run)
    return ApiResponse(data={"state": "running"}, message="Survey intelligence refresh started.")


@router.get("/panel/surveys", response_model=ApiResponse)
def panel_surveys():
    """Available surveys for the Panel & Volatility selector + load status."""
    return ApiResponse(data=pulse_panel.status())


@router.get("/panel", response_model=ApiResponse)
def panel(sids: str = ""):
    """Dynamic panel/volatility for a chosen survey set, e.g. ?sids=19,20,26,28,31.
    Panel = phones present in ALL selected surveys. Computed in-memory (<1s)."""
    try:
        chosen = [int(x) for x in sids.split(",") if x.strip().isdigit()]
    except ValueError:
        chosen = []
    return ApiResponse(data=pulse_panel.compute(chosen))


@router.post("/panel/reload", response_model=ApiResponse)
def panel_reload(background: BackgroundTasks):
    """Re-load the voter matrix into memory (call after re-materializing it)."""
    background.add_task(pulse_panel.load)
    return ApiResponse(data={"reloading": True}, message="Panel matrix reloading.")


@router.post("/constituencies/reload", response_model=ApiResponse)
def reload_constituency_cache(background: BackgroundTasks):
    """Clear + re-warm the in-process constituency cache (call after re-materializing
    so the next opens are instant on the fresh rows)."""
    invalidate_constituency_views()
    background.add_task(preload_constituency_views)
    return ApiResponse(data={"cleared": True, "rewarming": True}, message="Constituency cache cleared; re-warming.")


@router.get("/refresh/precheck", response_model=ApiResponse)
def refresh_precheck():
    """Run the refresh prechecks without recomputing — fast validation."""
    try:
        with dakavara_session() as dak_db:
            checks = PulseComputeRepository(dak_db).precheck()
        ok = all(c["ok"] for c in checks if c["critical"])
        return ApiResponse(data={"ok": ok, "checks": checks})
    except Exception as exc:
        logger.exception("api=pulse_precheck error=%s", str(exc))
        raise HTTPException(status_code=500, detail={
            "success": False, "error": "INTERNAL_SERVER_ERROR",
            "message": "pulse_precheck failed. Please check FastAPI logs."})


@router.post("/refresh", response_model=ApiResponse)
def refresh(background: BackgroundTasks, force: bool = False, x_refresh_token: str = Header(default=None)):
    _check_token(x_refresh_token)
    if pulse_cache.STATUS["state"] == "running":
        return ApiResponse(data=pulse_cache.STATUS, message="Refresh already running.")
    background.add_task(_run_refresh, force)
    return ApiResponse(data={"state": "running", "force": force}, message="Refresh started in background.")


@router.get("/refresh/status", response_model=ApiResponse)
def refresh_status():
    return ApiResponse(data=pulse_cache.STATUS)
