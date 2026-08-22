# Backend/main.py — Dashboard 2, the alternate Local Body Election console.
#
# A separate FastAPI app from ../../portal-frontend-code, which serves Dashboard 1 (and the
# whole nomination wizard). Same live dakavara_pa database, read-only here.
#
# Mounted at /portal-frontend-code-2 by ../../gateway.py; also runs standalone on port 4002.
#
# Layout mirrors ../../portal-dashboard:
#   config.py    env, DB settings
#   db.py        connection pooling, run() / read_cursor()
#   scope.py     the userLocationLevelId / userLocationLevelValuesStr pair -> assembly ids
#   queries.py   every SELECT, with the schema reasoning behind each
#   routers/     dashboard.py (the six screens), lookups.py (the picklists)
#
# --- No login, on purpose -------------------------------------------------
# Dashboard 1's backend is behind a bearer token and a session guard. This one is not: it
# has no /login, no /me, no middleware, and no user id anywhere. Scope arrives on every
# request as (userLocationLevelId, userLocationLevelValuesStr) and is a FILTER, not a
# permission — a caller can widen it at will. Do not put anything behind this service that
# is not safe to serve to whoever can reach the port, and do not add a write endpoint here
# without adding authentication first.
#
# --- Reads are open, writes are not ----------------------------------------
# Every GET is unauthenticated, exactly as this backend was built. The three POSTs in
# routers/writes.py require a valid portal session token (auth.py) and take the acting
# user id from it. Guarding the writes did NOT protect the reads. The writes (propose, confirm, upload nomination) stay in
# ../../portal-frontend-code/Backend/main.py, which owns assignProposalCandidate and the
# eligibility rules behind its 409s. A second copy of those rules here would drift.
#
# --- What this backend cannot answer --------------------------------------
# Dashboard 2 draws a seven-stage pipeline. dakavara_pa carries the first four
# (Not started / Proposal received / Confirmed / Nomination filed) and has no table at all
# for Door to Door, Door to Door - 2 or Result. Those fields are served as zeros and named
# in every response's `stagesUnavailable`. See routers/dashboard.py's EMPTY_STAGE_FIELDS.
#
# Run:  pip install -r ../requirements.txt
#       python main.py            (or: uvicorn main:app --port 4002)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import dashboard, lookups, writes

app = FastAPI(
    title="Local Body Elections - Dashboard 2 API",
    description=(
        "Read-only reporting API behind Dashboard 2. No authentication: every endpoint "
        "takes its own location scope as userLocationLevelId (5 Assembly / 4 Parliament / "
        "null State) plus userLocationLevelValuesStr."
    ),
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:9001",
        "http://127.0.0.1:9001",
        "http://localhost:8080",
        "https://www.mypartydashboard.com",
        "https://mypartydashboard.com",
        "https://portalnew.mypartydashboard.com",
    ],
    # False, not True: the session is a Bearer header, never a cookie, so no
    # request here carries credentials — and allow_credentials=True is what makes
    # Starlette refuse to echo an origin it would otherwise allow.
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(lookups.router)
app.include_router(dashboard.router)
app.include_router(writes.router)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": app.title}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4002)
