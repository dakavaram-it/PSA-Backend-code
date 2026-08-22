# Backend/auth.py — who is making this request.
#
# Dashboard 2 has no login of its own and issues no tokens. It VERIFIES the token
# ../../portal-frontend-code/Backend/main.py's /login already handed the browser: same
# secret, same algorithm, same 8-hour expiry. One login, one session, one credential store
# for the whole portal — a second login here would mean a second place to get password
# hashing, throttling and expiry right.
#
# WHAT THIS DOES AND DOES NOT COVER
# ---------------------------------
# The GET endpoints stay open, exactly as they were: this backend was built unauthenticated
# and its reads are unchanged. Only the writes in routers/writes.py are guarded. That is a
# deliberate, narrow change — do not read "the service has auth now" as "the reads are
# protected", because they are not.
#
# The (userLocationLevelId, userLocationLevelValuesStr) pair is still a FILTER and still not
# authorisation — see scope.py. A caller holding any valid portal token may write to any
# location this backend exposes. Per-assembly write permission would mean resolving the
# token's user against user_state_access_info / user_constituency_access_info the way
# ../../portal-frontend-code's user_access_assemblies() does; that is not wired here.
import base64

import jwt
from fastapi import HTTPException, Request

# Via config so the .env is loaded however this module is reached, the same way every
# other setting in this backend resolves.
from config import JWT_ALGORITHM, JWT_SECRET

# The portal signs with the **base64-decoded** JWT_SECRET, not its characters — jjwt's
# `signWith(alg, String)` contract, which ../../portal-frontend-code/Backend/main.py and
# ../../pc-meetings/backend/app/auth.py both follow. Verifying against the raw 15-character
# string is a different key, so every real portal token came back "Invalid session".
# Padded because the secret is stored unpadded; b64decode wants a multiple of four.
JWT_KEY = base64.b64decode(JWT_SECRET + "===")


def bearer_token(request: Request):
    """One transport: `Authorization: Bearer <token>` — the same one api.js replays."""
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def require_user(request: Request) -> int:
    """The acting user's id, or 401. Used as a FastAPI dependency on every write.

    The id comes from the signed token and never from the request body — a body-supplied
    user id would let any browser forge the audit trail, which is the same reason
    ../../portal-frontend-code stamps inserted_user_id server-side.
    """
    token = bearer_token(request)
    if not token:
        raise HTTPException(401, "Sign in to make changes")
    try:
        claims = jwt.decode(token, JWT_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session")
    subject = claims.get("sub")
    if subject is None or not str(subject).isdigit():
        raise HTTPException(401, "Invalid session")
    return int(subject)
