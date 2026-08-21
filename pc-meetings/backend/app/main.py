"""PC Meetings API.

The data source has been removed: the routes below hold the contract the React
app calls, and each answers 501 until it is wired to the party database.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, config
from .routers import assemblies, committees, meetings, programs, remarks, units

app = FastAPI(
    title="PC Meetings API",
    description="Committee meetings and programmes. Awaiting a data source.",
    version="3.0.0",
)

# Everything here is scoped to the caller's own assemblies, so every route needs
# to know who the caller is: the identity is resolved once on the way in and read
# back by `auth.caller_scope`. Nothing is public but the liveness probe and the
# docs — the figures below are the whole state's without a grant to narrow them.
PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def guard(request, call_next):
    if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    try:
        request.state.scope = auth.scope_for_user(auth.user_id_for_request(request))
    except HTTPException as exc:
        # Raised from middleware rather than a route, so FastAPI's own handler
        # never sees it — build the response here.
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = await call_next(request)
    # One user's slice of the party's data, keyed to a bearer token: never a
    # response a shared cache may hand to the next caller.
    response.headers["Cache-Control"] = "no-store"
    return response


# Registered after the guard on purpose: add_middleware prepends, so whatever goes
# on last ends up outermost. CORS has to wrap the guard rather than sit inside it,
# or the guard's 401 short-circuits before CORS runs and a cross-origin caller sees
# an opaque failure instead of the 401 the client keys its logout on.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "PUT"],
    allow_headers=["*"],
)

app.include_router(assemblies.router)
app.include_router(committees.router)
app.include_router(meetings.router)
app.include_router(programs.router)
app.include_router(remarks.router)
app.include_router(units.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
