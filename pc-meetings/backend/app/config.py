"""Settings for the meetings API.

Everything is overridable from the environment so the same code runs against
staging and production without a rebuild. Credentials come from the repo-root
`.env`, which is gitignored — never inline them here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/app/config.py -> backend/ -> repo root
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# The committee-meeting tables (`meetings`, `meeting_invitee`, …) live in the
# `mytdp` schema; `.env` carries the credentials but not the schema name.
DB = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "mytdp"),
}

DB_TIMEOUT_SECONDS = int(os.getenv("DB_TIMEOUT_SECONDS", "60"))

# How long the server waits for a metadata or row lock before erroring. Short on purpose:
# waiting on a lock is not work, and the read path retries once, so a long wait costs a
# worker twice over.
DB_LOCK_WAIT_SECONDS = int(os.getenv("DB_LOCK_WAIT_SECONDS", "10"))

# Programmes' own roster tables (`role`, `program_role`, …) live in this sibling
# schema, not `mytdp` — the same RDS user has cross-schema read access, so
# queries qualify the table name (`{PARTY_TRACK_DB}.role`) rather than opening
# a second connection.
PARTY_TRACK_DB = os.getenv("PARTY_TRACK_DB", "party_track")

# Committee meetings are `meeting_type_id` 1; type 2 is a different programme.
MEETING_TYPE_ID = int(os.getenv("MEETING_TYPE_ID", "1"))

# `booth.publication_id` scopes booths to the live electoral roll publication.
UNIT_PUBLICATION_ID = int(os.getenv("UNIT_PUBLICATION_ID", "42"))

# `leader.party_id` scopes the Programmes roster to this party — `leader` holds
# a handful of rows for other parties (872 is ~71.2k of ~71.3k total).
LEADER_PARTY_ID = int(os.getenv("LEADER_PARTY_ID", "872"))

# One invitee list can run to six figures of rows; the table cannot render that
# and the browser should not download it.
MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "3000"))

MAX_REMARKS_CHARS = int(os.getenv("MAX_REMARKS_CHARS", "2000"))

# --- Session ---------------------------------------------------------------
# This service mints nothing: the portal (portal-frontend-code/Backend) issues
# the token and both sides verify it with the same secret and algorithm. See
# auth.py for why the key is the base64-*decoded* secret.
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = os.getenv("ALGORITHM", "HS512").strip('"')

# A token stops being a session after this, counted from its `iat` — the same
# 8-hour window the portal's own SESSION_TTL uses, whatever `exp` says.
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(8 * 60 * 60)))

# The portal's schema, holding a user's assembly grants
# (user_state_access_info / user_constituency_access_info). Read cross-schema by
# the same RDS user, the same way PARTY_TRACK_DB is.
PORTAL_DB = os.getenv("PORTAL_DB_NAME", "dakavara_pa")

# How long a resolved grant is reused for. A grant changed in the portal lands
# within the window rather than at the next login.
ACCESS_CACHE_SECONDS = int(os.getenv("ACCESS_CACHE_SECONDS", "30"))

# --- File storage -----------------------------------------------------------
# S3, same account/bucket the portal's own `uploadNominationFile` uses
# (`portal-frontend-code/Backend/main.py`) — same env var names on purpose, so
# the two services can share one `.env` entry for the credentials once they
# are issued. `S3_ACCESS_KEY`/`S3_SECRET_KEY` are blank until then; see
# `storage.py` for what runs with no client configured.
S3_BUCKET = os.getenv("S3_BUCKET", "leader-reports")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")

# Vite dev server by default; set to the deployed frontend origin in production.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:5174"
    ).split(",")
    if o.strip()
]
