import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from core.errors import AppException, app_exception_handler, generic_exception_handler

try:
    from api.nominated_post_routes import router as nominated_post_router
except Exception as e:
    nominated_post_router = None
    logging.exception("Failed to import nominated_post_routes")

try:
    from api.nominated_proposal_routes import router as nominated_proposal_router
except Exception as e:
    nominated_proposal_router = None
    logging.exception("Failed to import nominated_proposal_routes")

try:
    from api.committee_routes import router as committee_router
except Exception:
    committee_router = None
    logging.exception("Failed to import committee_routes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("app.main")
# workflow services
try:
    from api.workflow_routes import router as workflow_router
except Exception as e:
    workflow_router = None
    logging.exception("Failed to import workflow_routes")

try:
    from api.cases_dashboard_routes import router as cases_dashboard_router
except Exception as e:
    cases_dashboard_router = None
    logging.exception("Failed to import cases_dashboard_routes")

try:
    from api.pulse_routes import router as pulse_router
except Exception as e:
    pulse_router = None
    logging.exception("Failed to import pulse_routes")

try:
    from api.meetings_routes import router as meetings_router
except Exception as e:
    meetings_router = None
    logging.exception("Failed to import meetings_routes")

try:
    from api.sir_dashboard_routes import router as sir_dashboard_router
except Exception as e:
    sir_dashboard_router = None
    logging.exception("Failed to import sir_dashboard_routes")

app = FastAPI(
    title="Membership Analytics Workflow API",
    description="FastAPI backend for Nominated Post / Committee proposal workflow",
    version="1.0.0",
)

app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

# =========================================================
# CORS Middleware
# =========================================================
# IMPORTANT:
# Add CORS middleware BEFORE custom middleware
# so every response gets CORS headers properly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# =========================================================
# Request Logging Middleware
# =========================================================
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):

    request_id = str(uuid.uuid4())
    start_time = time.time()

    logger.info(
        "request_start request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
    )

    try:
        response = await call_next(request)

        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        logger.info(
            "request_end request_id=%s status=%s elapsed_ms=%s",
            request_id,
            response.status_code,
            elapsed_ms,
        )

        response.headers["X-Request-ID"] = request_id

        return response

    except Exception as exc:

        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        logger.exception(
            "request_failed request_id=%s path=%s elapsed_ms=%s error=%s",
            request_id,
            request.url.path,
            elapsed_ms,
            str(exc),
        )

        # Re-raise exception so FastAPI handles properly
        raise exc


# =========================================================
# Global Exception Handler
# =========================================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):

    logger.exception("Unhandled exception: %s", str(exc))

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "INTERNAL_SERVER_ERROR",
            "message": str(exc),
        },
    )


# =========================================================
# Root API
# =========================================================
@app.get("/")
async def root():
    return {
        "success": True,
        "service": "Membership Analytics FastAPI Backend",
        "status": "running",
        "docs": "/docs",
    }


# =========================================================
# Health API
# =========================================================
@app.get("/health")
async def health():
    return {
        "success": True,
        "status": "UP",
        "service": "backend-python",
    }


# =========================================================
# Register Routers
# =========================================================
if nominated_post_router:

    app.include_router(
        nominated_post_router,
        prefix="/api/v1/nominated-post",
        tags=["Nominated Post"],
    )

    logger.info(
        "Nominated Post router registered at /api/v1/nominated-post"
    )

else:
    logger.error("Nominated Post router NOT registered")

if nominated_proposal_router:
    app.include_router(
        nominated_proposal_router,
        prefix="/api/v1/nominated-post",
        tags=["Nominated Post Proposal"],
    )
    logger.info("Nominated Proposal router registered at /api/v1/nominated-post")
else:
    logger.error("Nominated Proposal router NOT registered")

# =========================================================
# Workflow Routers
# =========================================================

if workflow_router:
    app.include_router(
        workflow_router,
        prefix="/api/v1/nominated-post",
        tags=["Nominated Post Workflow"],
    )
    logger.info("Workflow router registered at /api/v1/nominated-post")
else:
    logger.error("Workflow router NOT registered")

if cases_dashboard_router:
    app.include_router(
        cases_dashboard_router,
        prefix="/api/v1/cases",
        tags=["Cases Dashboard"],
    )
    logger.info("Cases Dashboard router registered at /api/v1/cases")
else:
    logger.error("Cases Dashboard router NOT registered")

if pulse_router:
    app.include_router(pulse_router, prefix="/api/v1/pulse", tags=["Pulse Trend"])
    logger.info("Pulse Trend router registered at /api/v1/pulse")
else:
    logger.error("Pulse Trend router NOT registered")

if meetings_router:
    app.include_router(meetings_router, prefix="/api/v1/meetings", tags=["Meetings"])
    logger.info("Meetings router registered at /api/v1/meetings")
else:
    logger.error("Meetings router NOT registered")

if sir_dashboard_router:
    app.include_router(sir_dashboard_router, prefix="/api/v1/sir-dashboard", tags=["SIR Dashboard"])
    logger.info("SIR Dashboard router registered at /api/v1/sir-dashboard")
else:
    logger.error("SIR Dashboard router NOT registered")

# =========================================================
# Committee Workflow Router
# =========================================================
if committee_router:
    app.include_router(
        committee_router,
        prefix="/api/v1/committee",
        tags=["Committee Workflow"],
    )
    logger.info("Committee router registered at /api/v1/committee")
else:
    logger.error("Committee router NOT registered")


# =========================================================
# Warm the Pulse constituency cache on startup (background)
# so first opens are instant, not just repeats.
# =========================================================
@app.on_event("startup")
def _warm_pulse_constituencies():
    import threading

    # Warm the master/tracking DB pools so the first real request doesn't pay the
    # multi-second cold-connect to the remote RDS (a major source of the slow /
    # "fails on first click" behaviour).
    def _warm_db():
        try:
            from db_config.db import dakavara_engine, pa_track_engine
            from sqlalchemy import text
            for eng in (dakavara_engine, pa_track_engine):
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
            logger.info("DB connection pools warmed (dakavara + pa_track)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("DB pool warm-up skipped: %s", exc)

    threading.Thread(target=_warm_db, daemon=True).start()

    def _run():
        try:
            from services.pulse_service import preload_constituency_views
            n, secs = preload_constituency_views()
            logger.info("Pulse constituency cache preloaded: %d seats in %ss", n, secs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pulse constituency preload skipped: %s", exc)

    threading.Thread(target=_run, daemon=True).start()

    # dynamic Panel & Volatility matrix → load into memory (background)
    def _run_panel():
        try:
            from services import pulse_panel
            pulse_panel.load()
            logger.info("Pulse panel matrix loaded: %d voters in %ss",
                        pulse_panel._STATE.get("rows"), pulse_panel._STATE.get("loadSec"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pulse panel matrix load skipped: %s", exc)

    threading.Thread(target=_run_panel, daemon=True).start()

    # Survey Intelligence snapshots → warm the in-process cache for every agency (background)
    def _run_survey():
        try:
            from api.pulse_routes import _survey_snapshot
            from services import pulse_survey
            for a in pulse_survey.agency_meta():
                if a["loaded"]:
                    snap = _survey_snapshot(a["id"])
                    logger.info("Survey snapshot warmed: %s (%d ACs)", a["id"], len((snap or {}).get("perAC", [])))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Survey snapshot warm skipped: %s", exc)

    threading.Thread(target=_run_survey, daemon=True).start()

    # Candidate Pool → build + cache the (~20-30s) report_ratings pool once in the
    # background so the first "Candidates Watchlist" navigation is instant.
    def _run_candidate_pool():
        try:
            from services.cadre_performance_service import CadrePerformanceService
            if not CadrePerformanceService.is_configured():
                return
            from db_config.db import dakavara_session, update_session, report_ratings_session
            from repositories.cadre_profile_repository import CadreProfileRepository
            from repositories.cadre_performance_repository import CadrePerformanceRepository
            t0 = time.time()
            with dakavara_session() as dak_db, update_session() as write_db, report_ratings_session() as rr_db:
                profile_repo = CadreProfileRepository(dak_db, write_db=write_db)
                service = CadrePerformanceService(profile_repo, CadrePerformanceRepository(rr_db))
                result = service.list_candidate_pool(limit=50000)
            logger.info("Candidate pool cache warmed: %d candidates in %.1fs",
                        result.get("total", 0), time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Candidate pool warm-up skipped: %s", exc)

    threading.Thread(target=_run_candidate_pool, daemon=True).start()