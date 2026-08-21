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

# Vite dev server by default; set to the deployed frontend origin in production.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://localhost:5174"
    ).split(",")
    if o.strip()
]
