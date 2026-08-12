# Backend/config.py — environment and DB connection settings.
# Reads this sub-directory's own .env (portal-dashboard/.env), resolved off this
# file's location so the app starts the same way whichever directory it's
# launched from — and so gateway.py's per-project load_dotenv() and a standalone
# run agree on which file wins. Same live dakavara_pa database as
# ../admin-dashboard, read-only here.
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
)

# --- connection pooling ----------------------------------------------------
# Same reasoning as ../admin-dashboard/Backend/config.py: the round trip to the remote RDS
# box is the expensive part, so connections are pooled and reused.
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
IDLE_REVALIDATE_SECONDS = 60
