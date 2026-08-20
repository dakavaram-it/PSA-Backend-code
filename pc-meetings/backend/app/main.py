"""PC Meetings API.

The data source has been removed: the routes below hold the contract the React
app calls, and each answers 501 until it is wired to the party database.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .routers import assemblies, committees, meetings, programs, remarks, units

app = FastAPI(
    title="PC Meetings API",
    description="Committee meetings and programmes. Awaiting a data source.",
    version="3.0.0",
)

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
