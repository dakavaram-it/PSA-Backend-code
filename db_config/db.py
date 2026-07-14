from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from core.config import get_settings

settings = get_settings()

# Recycle pooled connections well under the ~350s idle timeout that AWS network
# middleboxes (NAT gateway / NLB) impose. The RDS server's own wait_timeout is 8h,
# so it isn't the culprit — the network silently drops idle TCP sockets, and
# pre_ping alone can let such a half-open socket through on the first call after an
# idle period (the classic "fails on first click, works on the second"). Recycling
# every few minutes guarantees a fresh connection after any idle gap.
POOL_RECYCLE_SECONDS = 280

dakavara_engine = create_engine(
    settings.dakavara_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=POOL_RECYCLE_SECONDS,
    connect_args={"connect_timeout": 10, "read_timeout": 600, "write_timeout": 600},
)
# Same recycle/pre_ping treatment as dakavara_engine (see POOL_RECYCLE_SECONDS).
pa_track_engine = create_engine(
    settings.pa_track_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=POOL_RECYCLE_SECONDS,
    connect_args={"connect_timeout": 10, "read_timeout": 600, "write_timeout": 600},
)

# SIR form-count DB (separate RDS instance — mytdp). read_timeout kept generous
# for the heavier group-by analytics queries.
sir_engine = create_engine(
    settings.sir_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=POOL_RECYCLE_SECONDS,
    connect_args={"connect_timeout": 10, "read_timeout": 120, "write_timeout": 120},
)

# MY TDP app DB (mytdp on the projectk cluster) — feeds the candidate
# "MY TDP APP USAGE" panel. Heaviest sub-query (constituency_rank) runs in a few
# seconds, so the read_timeout is kept generous.
mytdp_engine = None
if settings.mytdp_configured:
    mytdp_engine = create_engine(
        settings.mytdp_url, pool_pre_ping=True, pool_size=3, max_overflow=5, pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args={"connect_timeout": 10, "read_timeout": 120, "write_timeout": 120},
    )

report_ratings_engine = None
if settings.report_ratings_configured:
    report_ratings_engine = create_engine(
        settings.report_ratings_url, pool_pre_ping=True, pool_size=3, max_overflow=5, pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args={"connect_timeout": 10, "read_timeout": 120, "write_timeout": 120},
    )

# Writable dakavara_pa engine — used ONLY for the caste/sub-caste update.
update_engine = None
if settings.update_db_configured:
    update_engine = create_engine(
        settings.update_url, pool_pre_ping=True, pool_size=2, max_overflow=3, pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args={"connect_timeout": 10, "read_timeout": 120, "write_timeout": 120},
    )

DakavaraSessionLocal = sessionmaker(bind=dakavara_engine, autocommit=False, autoflush=False)
SirSessionLocal = sessionmaker(bind=sir_engine, autocommit=False, autoflush=False)
PaTrackSessionLocal = sessionmaker(bind=pa_track_engine, autocommit=False, autoflush=False)
MyTdpSessionLocal = (
    sessionmaker(bind=mytdp_engine, autocommit=False, autoflush=False)
    if mytdp_engine
    else None
)
ReportRatingsSessionLocal = (
    sessionmaker(bind=report_ratings_engine, autocommit=False, autoflush=False)
    if report_ratings_engine
    else None
)
UpdateSessionLocal = (
    sessionmaker(bind=update_engine, autocommit=False, autoflush=False)
    if update_engine
    else None
)


@contextmanager
def dakavara_session():
    db = DakavaraSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def sir_session():
    db = SirSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def pa_track_session():
    db = PaTrackSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def mytdp_session():
    """MY TDP app DB session (mytdp on the projectk cluster). Yields None when the
    connection isn't configured so callers can degrade gracefully."""
    if not MyTdpSessionLocal:
        yield None
        return
    db = MyTdpSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def update_session():
    """Writable dakavara_pa session for the caste/sub-caste update only.
    Yields None when the UPDATE_DB_* credentials are not configured."""
    if not UpdateSessionLocal:
        yield None
        return
    db = UpdateSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def report_ratings_session():
    if not ReportRatingsSessionLocal:
        raise RuntimeError("report_ratings database is not configured")
    db = ReportRatingsSessionLocal()
    try:
        yield db
    finally:
        db.close()


def health_check() -> dict:
    result = {}
    with dakavara_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    result["dakavara_pa"] = "connected"
    with pa_track_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    result["pa_track"] = "connected"
    if report_ratings_engine:
        with report_ratings_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        result["report_ratings"] = "connected"
    else:
        result["report_ratings"] = "not_configured"
    return result
