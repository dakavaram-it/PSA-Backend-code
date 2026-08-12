# Backend/main.py — KPI/lookup API behind the admin-dashboard frontend's
# "Portal Dashboard" nav item. A separate FastAPI app from ../admin-dashboard,
# which serves that frontend's "Dashboard" item: different tables entirely
# (`user`/`team_type`/`entitlement`, not `activity_member`/`user_type`), same
# live dakavara_pa database — see config.py.
#
# Mounted at /portal-dashboard by ../gateway.py; also runs standalone on port
# 4001, the port the frontend's /portalapi proxy falls back to.
#
# Layout mirrors the admin dashboard:
#   config.py    env, DB settings
#   db.py        connection pooling, run() / run_write_tx()
#   queries.py   the SELECT statements the read endpoints are built on
#   schemas.py   request bodies (Pydantic)
#   services.py  the Detail screen's write helpers
#   routers/     stats.py, lookups.py, users.py
#
# Almost everything here is SELECT-only. The writes live in routers/users.py:
# PUT /api/portal/users/{user_id} (a real `user` row's access_type/access_value
# and is_enabled, staged as one draft on the Detail screen and saved in one
# request) and POST /api/portal/users (Create New User — one INSERT, no
# separate identity table the way the admin dashboard's tdp_cadre+activity_member
# pair needs) — plus routers/entitlements.py's POST /api/portal/entitlements
# (Entitlement Management's Create Entitlement, another single-table INSERT).
#
# Run:  pip install -r ../requirements.txt
#       python main.py            (or: uvicorn main:app --port 4001)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import entitlements, lookups, stats, users

app = FastAPI(title="Portal stats API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST"],
    allow_headers=["*"],
)

app.include_router(stats.router)
app.include_router(lookups.router)
app.include_router(users.router)
app.include_router(entitlements.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4001)
