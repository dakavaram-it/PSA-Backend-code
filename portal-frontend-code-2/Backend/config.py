# Backend/config.py — environment and DB connection settings.
# Copied from ../../portal-dashboard/Backend/config.py: same RDS box, same pooling
# reasoning, and the gateway loads both the same way. Read-only here — Dashboard 2
# writes nothing.
# Reads this sub-directory's own .env (portal-dashboard/.env), resolved off this
# file's location so the app starts the same way whichever directory it's
# launched from — and so gateway.py's per-project load_dotenv() and a standalone
# run agree on which file wins. Same live dakavara_pa database as
# ../admin-dashboard and ../portal-frontend-code, read-only here.
import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

DB = dict(
    host=os.environ["DB_HOST"],
    port=int(os.environ["DB_PORT"]),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ["DB_NAME"],
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=15,
    read_timeout=60,
    # The server's global lock_wait_timeout is 31536000 (a year), so a metadata lock
    # held by another session — a DDL, or an ETL job parked in a transaction — parks
    # every query on this connection until someone notices. That is not theoretical:
    # 220 psa_user queries were stuck in "Waiting for table metadata lock" on
    # dakavara_pa/mytdp for over an hour, threads and pool slots pinned the whole time,
    # when this was added. Ten seconds is long enough for a real lock hand-off and short
    # enough that a jam errors as 1205 on the one request that hit it instead of draining
    # the pool. Same pragma, same reasoning as ../../portal-frontend-code/Backend/main.py
    # and ../../pc-meetings/backend/app/db.py, which already set it.
    init_command="SET SESSION lock_wait_timeout = "
    + os.environ.get("DB_LOCK_WAIT_TIMEOUT", "10"),
)

# --- connection pooling ----------------------------------------------------
# Same reasoning as ../admin-dashboard/Backend/config.py: the round trip to the remote RDS
# box is the expensive part, so connections are pooled and reused.
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
IDLE_REVALIDATE_SECONDS = 60

# --- session token -----------------------------------------------------------
# Dashboard 2 issues no tokens; it verifies the one ../../portal-frontend-code's /login
# handed the browser. Both values must match that project's .env or every write 401s.
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = os.environ.get("ALGORITHM", "HS512")
