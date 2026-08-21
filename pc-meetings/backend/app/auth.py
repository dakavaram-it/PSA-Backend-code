"""Who is calling, and which assemblies the portal has granted them.

This service mints nothing. The portal (`portal-frontend-code/Backend`) issues a
signed JWT at `/login`; every route here takes that same token as
`Authorization: Bearer <token>` and verifies it with the same secret and
algorithm. Three things are copied from that file deliberately, and have to move
with it:

* **The key is the base64-*decoded* `JWT_SECRET`, not its characters** — that is
  the jjwt `signWith(alg, String)` contract the Java portal signs with. Handing
  PyJWT the raw secret verifies against a different key and rejects every real
  token.
* **`sub` is the user id, as a string**, and is the whole payload's worth of
  identity. Everything else is read fresh here.
* **A token stops being a session after `SESSION_TTL` counted from its `iat`**,
  whatever `exp` says — the Java portal signs for 15 days, which is that
  portal's window and not this service's.

The grant itself is the portal's `user_access_assemblies` query, run here
against `dakavara_pa` rather than proxied back: this service already holds a
pooled connection to the same RDS host under a user with cross-schema read
access (the same access `config.PARTY_TRACK_DB` relies on), and a round trip to
the portal on every request would cost more than the query does.
"""

from __future__ import annotations

import base64
import threading
import time

import jwt
from fastapi import HTTPException, Request

from . import config, db
from .access import Scope

PORTAL_DB = config.PORTAL_DB
JWT_ALGORITHM = config.JWT_ALGORITHM
SESSION_TTL = config.SESSION_TTL_SECONDS

# Padded because the portal's secret is stored unpadded; b64decode wants a
# multiple of four and ignores surplus '='.
_KEY = base64.b64decode(config.JWT_SECRET + "===") if config.JWT_SECRET else b""


# Resolving a grant is two queries against another schema, and a single screen
# here fires a dozen requests. Memoised for a few seconds, the same trade the
# portal makes with its own identity cache: a grant changed in the portal lands
# within the window rather than at the next login.
_TTL_SECONDS = config.ACCESS_CACHE_SECONDS
_cache: dict[int, tuple[float, Scope]] = {}
_cache_lock = threading.Lock()


# One row in `user_state_access_info` is every assembly in that state. The
# meetings roster is one state's, so such a user is simply unrestricted here —
# which also keeps their queries free of a 175-id `IN` list they would all match.
_STATE_GRANT = f"SELECT 1 FROM {PORTAL_DB}.user_state_access_info WHERE user_id = %s LIMIT 1"

# The other two thirds of the portal's `user_access_assemblies`: a row in
# `user_constituency_access_info` is either a parliament (covering the
# assemblies whose `parliament_id` points at it) or one assembly itself.
# `election_scope.election_type_id = 2` is what makes a constituency row an
# assembly rather than a parliament or a local body.
_ASSEMBLY_GRANTS = f"""
SELECT C.constituency_id
  FROM {PORTAL_DB}.user_constituency_access_info CA
  JOIN {PORTAL_DB}.constituency C ON CA.constituency_id = C.parliament_id
  JOIN {PORTAL_DB}.election_scope ES ON C.election_scope_id = ES.election_scope_id
 WHERE CA.user_id = %s AND C.deform_date IS NULL AND ES.election_type_id = 2
 UNION
SELECT C.constituency_id
  FROM {PORTAL_DB}.user_constituency_access_info CA
  JOIN {PORTAL_DB}.constituency C ON CA.constituency_id = C.constituency_id
  JOIN {PORTAL_DB}.election_scope ES ON C.election_scope_id = ES.election_scope_id
 WHERE CA.user_id = %s AND C.deform_date IS NULL AND ES.election_type_id = 2
"""


def _resolve(user_id: int) -> Scope:
    if db.scalar(_STATE_GRANT, (user_id,)) is not None:
        return Scope(None)
    return Scope([r["constituency_id"] for r in db.rows(_ASSEMBLY_GRANTS, (user_id, user_id))])


def scope_for_user(user_id: int) -> Scope:
    """This user's assemblies. No grants at all is an empty scope, never the
    whole state — the same rule the portal's picklist follows."""
    now = time.monotonic()
    hit = _cache.get(user_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    resolved = _resolve(user_id)
    with _cache_lock:
        if len(_cache) > 512:
            for key in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(key, None)
        _cache[user_id] = (now + _TTL_SECONDS, resolved)
    return resolved


def user_id_for_request(request: Request) -> int:
    """The caller's user id, or 401. One transport: a bearer token."""
    if not _KEY:
        # Refusing is the only safe answer: with no key every token below would
        # fail to verify anyway, and answering unscoped would serve the whole
        # state to anyone who asked.
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured")

    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    token = value.strip() if scheme.lower() == "bearer" else ""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = jwt.decode(token, _KEY, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        # Expired, tampered with, signed by another key, or not a JWT at all —
        # every one of those is simply "no session" here.
        raise HTTPException(status_code=401, detail="Not authenticated") from None

    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)) or time.time() - issued_at >= SESSION_TTL:
        raise HTTPException(status_code=401, detail="Not authenticated")

    subject = str(claims.get("sub", ""))
    if not subject.isdigit():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return int(subject)


def caller_scope(request: Request) -> Scope:
    """FastAPI dependency: the assemblies this request may be answered with.

    The guard in `main.py` has already resolved and stashed it, so this is a
    read — but it stays a dependency so each route declares that it is scoped
    rather than reaching into `request.state` itself.
    """
    scope = getattr(request.state, "scope", None)
    if scope is None:  # pragma: no cover - the guard runs first on every route
        scope = scope_for_user(user_id_for_request(request))
        request.state.scope = scope
    return scope
