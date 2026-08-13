"""Self-check for the auth fixes in main.py. No DB, no test framework:

    cd Backend && python test_auth.py

Covers the failure modes that are hard to reproduce by clicking: a malformed `user`
row aborting a login that should have succeeded, and the two dicts that grow forever.
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


def user_row(salt_key, hash_key, user_id=1):
    return {
        "user_id": user_id,
        "username": USERNAME,
        "firstname": "Test",
        "lastname": "User",
        "user_type": 1,
        "state_id": 1,
        "district_id": 1,
        "constituency_id": 181,
        "Salt_Key": salt_key,
        "Hash_Key": hash_key,
    }


def matching_row(user_id=2):
    return user_row(
        GOOD_SALT, main.password_hash(USERNAME, PASSWORD, GOOD_SALT).upper(), user_id
    )


def login_against(rows, password=PASSWORD):
    """Run main.login with the `user` SELECT stubbed out to return `rows`.

    login() makes a second read for the entitlements once a password matches, so the
    stub answers on the SQL rather than handing `rows` to every caller."""
    real_query, main.query = main.query, lambda sql, args=None: (
        rows if "FROM `user`" in sql else [{"entitlement_name": "TEST_ENTITLEMENT"}]
    )
    try:
        return main.login(main.LoginRequest(username=USERNAME, password=password))
    finally:
        main.query = real_query


def request_with(headers=None):
    """The one attribute current_user/bearer_token reads off a Starlette Request."""
    return types.SimpleNamespace(headers=headers or {})


def bearer(user, ttl=60):
    """A signed token for `user`, expiring in `ttl` seconds (negative = already dead)."""
    return {
        "authorization": "Bearer "
        + jwt.encode(
            {**user, "exp": int(time.time()) + ttl},
            main.JWT_SECRET,
            algorithm=main.JWT_ALGORITHM,
        )
    }


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


def test_login_returns_a_token_carrying_the_user():
    # The token IS the session, so everything /me answers has to be inside it.
    reset()
    user = login_against([matching_row()])
    claims = jwt.decode(
        user["token"], main.JWT_SECRET, algorithms=[main.JWT_ALGORITHM]
    )
    assert claims["user_id"] == 2, claims
    assert claims["username"] == USERNAME
    # Signed for SESSION_TTL, give or take the second the login took.
    assert abs(claims["exp"] - (time.time() + main.SESSION_TTL)) < 5, claims["exp"]


def test_the_login_token_authenticates_and_hides_jwt_bookkeeping():
    reset()
    user = login_against([matching_row()])
    request = request_with(headers={"authorization": f"Bearer {user['token']}"})
    identity = main.current_user(request)
    assert "exp" not in identity, identity
    assert identity == {k: v for k, v in user.items() if k != "token"}, identity


def test_login_returns_entitlements_and_carries_them_in_the_token():
    # They gate what the portal shows, so a reload (me, i.e. the token) has to know
    # them too — not just the login response.
    reset()
    user = login_against([matching_row()])
    assert user["entitlements"] == ["TEST_ENTITLEMENT"], user
    claims = jwt.decode(user["token"], main.JWT_SECRET, algorithms=[main.JWT_ALGORITHM])
    assert claims["entitlements"] == ["TEST_ENTITLEMENT"], claims


def test_expired_token_is_rejected():
    reset()
    assert main.current_user(request_with(bearer({"user_id": 7}, ttl=-1))) is None


def test_tampered_or_foreign_tokens_are_rejected():
    reset()
    good = jwt.encode(
        {"user_id": 7, "exp": int(time.time()) + 60},
        main.JWT_SECRET,
        algorithm=main.JWT_ALGORITHM,
    )
    forged = jwt.encode(
        {"user_id": 7, "exp": int(time.time()) + 60},
        "not-the-secret",
        algorithm=main.JWT_ALGORITHM,
    )
    # Same claims, wrong key — the signature is the only thing standing between a
    # caller and any user_id they care to name.
    assert main.current_user(request_with({"authorization": f"Bearer {forged}"})) is None
    head, payload, sig = good.split(".")
    for broken in (f"{head}.{payload}.{sig[:-1]}x", f"{head}.{payload}", "not-a-jwt"):
        request = request_with({"authorization": f"Bearer {broken}"})
        assert main.current_user(request) is None, broken


def test_bearer_header_authenticates():
    reset()
    assert main.current_user(request_with(bearer({"user_id": 7}))) == {"user_id": 7}


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
