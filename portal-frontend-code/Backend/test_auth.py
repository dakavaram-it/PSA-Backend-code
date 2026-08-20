"""Self-check for the auth fixes in main.py. No DB, no test framework:

    cd Backend && python test_auth.py

Covers the failure modes that are hard to reproduce by clicking: a malformed `user`
row aborting a login that should have succeeded, the two dicts that grow forever, and
the portal handshake — which key its tokens verify against, and what a `sub`-only
payload turns back into.
"""

import binascii
import time
import types

import jwt
from fastapi import HTTPException

import main

USERNAME = "test.user"
PASSWORD = "correct horse"
GOOD_SALT = b"[B@3da6a354".hex()


def user_row(salt_key, hash_key, user_id=1, access_type="MLA", access_value="181"):
    return {
        "user_id": user_id,
        "username": USERNAME,
        "firstname": "Test",
        "lastname": "User",
        "user_type": 1,
        "access_type": access_type,
        "access_value": access_value,
        "Salt_Key": salt_key,
        "Hash_Key": hash_key,
    }


def matching_row(user_id=2):
    return user_row(
        GOOD_SALT, main.password_hash(USERNAME, PASSWORD, GOOD_SALT).upper(), user_id
    )


def login_against(rows, password=PASSWORD):
    """Run main.login with the `user` SELECT stubbed out to return `rows`.

    login() makes three more reads once a password matches — the entitlements, the
    hierarchy above the row's access_value, and the assemblies the user may work in — so
    the stub answers on the SQL rather than handing `rows` to every caller."""

    def stub(sql, args=None):
        if "FROM `user`" in sql:
            return rows
        if "FROM user_state_access_info" in sql:
            return [{"constituency_id": 181, "constituency_name": "Test Assembly"}]
        if "FROM constituency" in sql:
            return [{"state_id": 1, "district_id": 11}]
        if "FROM district" in sql:
            return [{"state_id": 1}]
        return [{"entitlement_id": 7, "entitlement_name": "TEST_ENTITLEMENT"}]

    real_query, main.query = main.query, stub
    try:
        return main.login(main.LoginRequest(username=USERNAME, password=password))
    finally:
        main.query = real_query


def request_with(headers=None):
    """The one attribute current_user/bearer_token reads off a Starlette Request."""
    return types.SimpleNamespace(headers=headers or {})


def bearer(user_id, ttl=60, iat_offset=0):
    """A portal-shaped header for `user_id`, expiring in `ttl` seconds (negative = already
    dead). `iat_offset` ages the token without touching its `exp`, which is how a token
    the portal still considers live falls outside this API's SESSION_TTL."""
    issued = int(time.time()) + iat_offset
    return {
        "authorization": "Bearer "
        + jwt.encode(
            {"sub": str(user_id), "iat": issued, "exp": int(time.time()) + ttl},
            main.JWT_KEY,
            algorithm=main.JWT_ALGORITHM,
        )
    }


def resolving(user_id, fn):
    """Run `fn` with the identity read current_user makes stubbed out, so the tests stay
    off the database."""
    identity = {"user_id": user_id, "username": USERNAME, "entitlements": []}
    real, main.identity_for = main.identity_for, lambda uid: (
        identity if uid == user_id else None
    )
    try:
        return fn()
    finally:
        main.identity_for = real


def reset():
    main.LOGIN_ATTEMPTS.clear()


def test_bad_salt_does_not_block_a_valid_login():
    # A Salt_Key that is not valid even-length hex used to raise out of the loop and
    # 500 the request, so the matching row behind it was never reached.
    reset()
    user = login_against([user_row("zz-not-hex", "whatever"), matching_row()])
    assert user["user_id"] == 2, user
    assert user["token"]


def test_bytes_hash_key_does_not_block_a_valid_login():
    # A BINARY/BLOB Hash_Key comes back as bytes; .lower() on it yields bytes, and
    # compare_digest(str, bytes) raises TypeError.
    reset()
    user = login_against([user_row(GOOD_SALT, b"\xde\xad\xbe\xef"), matching_row()])
    assert user["user_id"] == 2, user


def test_wrong_password_still_fails():
    # The guard must skip unusable rows, not treat them as matches.
    reset()
    for rows in ([matching_row()], [user_row("zz-not-hex", "whatever")], []):
        try:
            login_against(rows, password="wrong")
        except HTTPException as exc:
            assert exc.status_code == 401, exc.status_code
        else:
            raise AssertionError(f"expected 401 for rows={rows}")


def test_unknown_username_burns_a_hash():
    # Equal work for an unknown username as for a known one, so the `user` table is
    # not enumerable by timing. Asserting on wall-clock would be flaky; assert instead
    # that the dummy salt is usable, which is what the timing fix relies on.
    assert main.password_hash(USERNAME, PASSWORD, main.DUMMY_SALT_KEY)
    try:
        binascii.unhexlify(main.DUMMY_SALT_KEY)
    except binascii.Error:
        raise AssertionError("DUMMY_SALT_KEY must be valid hex")


def test_sweep_drops_only_stale_login_attempts():
    reset()
    now = time.time()
    main.LOGIN_ATTEMPTS["recent"] = [now - 1]
    main.LOGIN_ATTEMPTS["old"] = [now - main.LOGIN_WINDOW - 1]
    main.LOGIN_ATTEMPTS["empty"] = []

    main.sweep_expired(now)

    assert set(main.LOGIN_ATTEMPTS) == {"recent"}, main.LOGIN_ATTEMPTS


def test_the_login_token_is_shaped_the_way_the_portals_is():
    # The whole point of minting rather than fetching: the payload has to be what
    # getToken produces, or the portal rejects a token this API issued. `sub` is the
    # matched row's id, as a *string*, and it is signed for SESSION_TTL.
    reset()
    user = login_against([matching_row()])
    claims = jwt.decode(user["token"], main.JWT_KEY, algorithms=[main.JWT_ALGORITHM])
    assert claims["sub"] == "2", claims
    assert set(claims) == {"sub", "iat", "exp"}, claims
    assert claims["exp"] - claims["iat"] == main.SESSION_TTL, claims


def test_the_login_token_authenticates_as_the_same_identity():
    reset()
    user = login_against([matching_row()])
    request = request_with(headers={"authorization": f"Bearer {user['token']}"})
    identity = resolving(2, lambda: main.current_user(request))
    assert identity["user_id"] == 2, identity
    # The token carries `sub` and nothing else — none of the identity rides along.
    claims = jwt.decode(user["token"], main.JWT_KEY, algorithms=[main.JWT_ALGORITHM])
    assert set(claims) == {"sub", "iat", "exp"}, claims


def test_login_returns_entitlements():
    # They gate what the portal shows. They no longer ride the token, so /me re-reads
    # them; a grant changed in the DB now takes effect on the next request.
    reset()
    user = login_against([matching_row()])
    assert user["entitlements"] == [
        {"entitlement_id": 7, "entitlement_name": "TEST_ENTITLEMENT"}
    ], user


def test_login_returns_the_access_pair_and_the_assemblies():
    # The pair says how the user is scoped, which the three expanded ids cannot express;
    # the assemblies are the grants a query may actually be scoped by.
    reset()
    user = login_against([matching_row()])
    assert user["access_type"] == "MLA", user
    assert user["access_value"] == "181", user
    assert user["assemblies"] == [
        {"constituency_id": 181, "constituency_name": "Test Assembly"}
    ], user


def test_login_answers_the_scope_the_access_pair_names():
    # The regression this guards: /login read `user`.state_id/district_id/constituency_id,
    # which the Java portal never fills, so every response carried three nulls.
    reset()
    user = login_against([matching_row()])
    assert user["constituency_id"] == 181, user
    assert user["district_id"] == 11, user
    assert user["state_id"] == 1, user


def test_the_portals_own_token_verifies_here():
    # The whole point of the handoff: a token minted by
    # mypartydashboard.com/PSA/WebService/User/getToken is a session here. It verifies
    # against the **base64-decoded** JWT_SECRET, the way jjwt signs it — handing PyJWT
    # the raw characters rejects every token the portal issues.
    reset()
    sample = (
        "eyJhbGciOiJIUzUxMiJ9.eyJzdWIiOiIxIiwiZXhwIjoxNzg4NDcwOTEzLCJpYXQiOjE3ODcxNzQ5"
        "MTN9.sTijvCAc8ty5wQYiu1Xhcmyv-4qmVgOsBglsyTlxH4YLjRbW7-VqVC9Rw9FlT_b32Fzpqwpu"
        "ca_uQ915xj3_7A"
    )
    claims = jwt.decode(
        sample,
        main.JWT_KEY,
        algorithms=[main.JWT_ALGORITHM],
        options={"verify_exp": False},
    )
    assert claims["sub"] == "1", claims


def test_a_token_past_session_ttl_is_not_a_session():
    # getToken signs for 15 days. A token the portal still considers live is not one
    # here once its `iat` falls outside SESSION_TTL.
    reset()
    fresh = bearer(7, ttl=15 * 24 * 3600)
    assert resolving(7, lambda: main.current_user(request_with(fresh))) is not None
    stale = bearer(7, ttl=15 * 24 * 3600, iat_offset=-main.SESSION_TTL - 60)
    assert resolving(7, lambda: main.current_user(request_with(stale))) is None


def test_a_token_for_an_unknown_user_is_not_a_session():
    # `sub` is a claim, not proof the row still exists — a deleted user must not keep
    # a live token working.
    reset()
    request = request_with(bearer(4242))
    assert resolving(7, lambda: main.current_user(request)) is None


def test_scope_for_fills_in_only_what_the_access_level_names():
    # One level per branch, plus the rows whose access_value is not an id at all.
    real_query, main.query = main.query, lambda sql, args=None: (
        [{"state_id": 1, "district_id": 11}] if "FROM constituency" in sql
        else [{"state_id": 1}]
    )
    try:
        assert main.scope_for("MLA", "111") == {
            "state_id": 1, "district_id": 11, "constituency_id": 111
        }
        # A parliament spans districts: the constituency row's district_id is NULL.
        real_district = main.query
        main.query = lambda sql, args=None: [{"state_id": 1, "district_id": None}]
        assert main.scope_for("MP", "504") == {
            "state_id": 1, "district_id": None, "constituency_id": 504
        }
        main.query = real_district
        assert main.scope_for("DISTRICT", "11") == {
            "state_id": 1, "district_id": 11, "constituency_id": None
        }
        empty = {"state_id": None, "district_id": None, "constituency_id": None}
        assert main.scope_for("STATE", "1") == {**empty, "state_id": 1}
        assert main.scope_for("ZONE", "3") == empty
        assert main.scope_for("accessType", "accessValue") == empty
        assert main.scope_for("MLA", None) == empty
    finally:
        main.query = real_query


def test_expired_token_is_rejected():
    reset()
    assert main.current_user(request_with(bearer(7, ttl=-1))) is None


def test_tampered_or_foreign_tokens_are_rejected():
    reset()
    good = jwt.encode(
        {"sub": "7", "iat": int(time.time()), "exp": int(time.time()) + 60},
        main.JWT_KEY,
        algorithm=main.JWT_ALGORITHM,
    )
    forged = jwt.encode(
        {"sub": "7", "iat": int(time.time()), "exp": int(time.time()) + 60},
        "not-the-secret",
        algorithm=main.JWT_ALGORITHM,
    )
    # The raw JWT_SECRET characters are a wrong key too: the portal signs with its
    # base64-decoded bytes, and the signature is the only thing standing between a
    # caller and any user_id they care to name.
    raw_secret = jwt.encode(
        {"sub": "7", "iat": int(time.time()), "exp": int(time.time()) + 60},
        main.env("JWT_SECRET"),
        algorithm=main.JWT_ALGORITHM,
    )
    for bad in (forged, raw_secret):
        request = request_with({"authorization": f"Bearer {bad}"})
        assert resolving(7, lambda: main.current_user(request)) is None, bad
    head, payload, sig = good.split(".")
    for broken in (f"{head}.{payload}.{sig[:-1]}x", f"{head}.{payload}", "not-a-jwt"):
        request = request_with({"authorization": f"Bearer {broken}"})
        assert main.current_user(request) is None, broken


def test_bearer_header_authenticates():
    reset()
    request = request_with(bearer(7))
    identity = resolving(7, lambda: main.current_user(request))
    assert identity["user_id"] == 7, identity


def test_a_cookie_no_longer_authenticates():
    # The cookie transport is gone; only the Authorization header is read.
    reset()
    request = types.SimpleNamespace(headers={}, cookies={"lbe_session": "anything"})
    assert main.current_user(request) is None


def test_malformed_authorization_headers_are_not_a_session():
    reset()
    for header in ("", "Bearer", "Bearer   ", "Basic tok", "tok"):
        assert (
            main.current_user(request_with({"authorization": header})) is None
        ), header


def test_logout_answers_ok_without_a_request():
    # Stateless: there is nothing server-side to drop, and the endpoint takes no args.
    assert main.logout() == {"ok": True}


def test_cors_wraps_the_auth_guard():
    # add_middleware prepends and index 0 is outermost, so CORS must be registered
    # last or a 401 from the guard comes back without CORS headers.
    names = [m.cls.__name__ for m in main.app.user_middleware]
    assert names[0] == "CORSMiddleware", names


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    reset()
    print("all passed")
