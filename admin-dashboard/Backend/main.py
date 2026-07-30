# Backend/main.py — data layer for the UA admin console.
# Python + FastAPI + PyMySQL. Powers the frontend with real dakavara_pa data.
#
# Layout:
#   config.py    env, DB settings, product constants
#   db.py        connection pooling, run() / run_write_tx()
#   queries.py   the SELECT statements the read endpoints are built on
#   schemas.py   request bodies
#   services.py  shared member logic (grant mutation, post-write re-read, OTP)
#   routers/     members.py, lookups.py, cadre.py, auth.py
#
# Most endpoints are SELECT-only. The write endpoints cover full CRUD for a
# login (activity_member + its access_type/access_level/component grants):
# POST /api/members creates a login (and grants) for a cadre that doesn't have
# one yet; PUT .../role, .../level and .../active update an existing login's
# role, geographic scope and active flag; PUT /api/members/{id} applies a whole
# Detail-screen save in one transaction; POST/DELETE .../components grant and
# revoke a single personal component; and DELETE /api/members/{id}
# soft-deletes a login by cascading is_active/is_valid='N' across every grant
# table (distinct from deactivate, which only flips activity_member.is_acitve
# and leaves grants intact for a later reactivate).
#
# POST /api/login checks the console's single operator account against
# LOGIN_USERNAME/LOGIN_PASSWORD in .env (routers/auth.py). It gates the UI only
# — it issues no session token and the endpoints above still accept any caller,
# so anyone who can reach this port can call the write endpoints directly. Put
# this behind real auth before it's exposed outside a trusted network.
#
# Run:  pip install -r requirements.txt
#       python main.py            (or: uvicorn main:app --port 4000)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, cadre, lookups, members

app = FastAPI(title="UA admin API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],
    allow_headers=["*"],
)

app.include_router(members.router)
app.include_router(lookups.router)
app.include_router(cadre.router)
app.include_router(auth.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4000)
