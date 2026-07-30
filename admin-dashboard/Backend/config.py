# Backend/config.py — environment, DB connection settings and the product
# constants the rest of the backend reads. Everything here is process-wide and
# loaded once at import.
import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv

# Repo-root .env (git-ignored — holds live credentials). Resolved off this
# file's location rather than the working directory, so the app starts the same
# way whether it's launched from Backend/ or from the repo root.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

DB = dict(
    host=os.environ["DB_HOST"],
    port=int(os.environ["DB_PORT"]),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ["DB_NAME"],
    charset="utf8mb4",              # component labels include Telugu
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=15,
    read_timeout=60,
)

# --- admin console login ---------------------------------------------------
# The console has exactly one operator account and .env is its only definition
# — no users table, no fallback pair, no bypass. Read with os.environ[...]
# rather than .get(): a missing value must stop the app at import, not quietly
# become an empty credential.
LOGIN_USERNAME = os.environ["LOGIN_USERNAME"]
LOGIN_PASSWORD = os.environ["LOGIN_PASSWORD"]

# --- connection pooling ----------------------------------------------------
# The database is a remote RDS instance and the round trip to it is expensive:
# a fresh handshake measures ~1.1 s and the SET SESSION pragmas another ~0.4 s,
# against ~0.2 s for an actual query. Connections are pooled and reused so the
# pragmas run once per connection rather than once per query, and a request
# shares one connection across all of its queries.
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))

# A pooled connection can be dropped by the server or the network while it sits
# idle. Anything idle longer than this is pinged before reuse, so a dead
# connection is replaced instead of surfacing as a spurious request failure.
# (wait_timeout on this server is 8 h, so this almost never fires in practice.)
IDLE_REVALIDATE_SECONDS = 60

# S3 bucket cadre photos are stored under — tdp_cadre.image holds just the
# relative key (e.g. "152/AP1406574091.jpg"), NULL for cadre with no photo.
CADRE_IMAGE_BASE = "https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/"

# Placeholder image key stamped on every cadre record created from the admin
# console's manual "Create Membership ID" flow — this console has no photo
# upload, so every such record gets the same stand-in key rather than NULL.
DEFAULT_CADRE_IMAGE = "human.jpg"

# login_otp_details has no expiry column. By product decision the admin sets an
# OTP's expiry explicitly (Reset OTP modal's date/time picker) and it is stored
# in that row's updated_time — generated_time stays the true creation
# timestamp, untouched. OTP_DEFAULT_VALID_MINUTES is only the fallback used
# when the admin doesn't pick a time.
OTP_DEFAULT_VALID_MINUTES = 10
