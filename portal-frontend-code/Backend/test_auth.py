"""Self-check for the auth fixes in main.py. No DB, no test framework:

    cd Backend && python test_auth.py

Covers the failure modes that are hard to reproduce by clicking: a malformed `user`
row aborting a login that should have succeeded, and the two dicts that grow forever.
"""

import binascii
import time
import types

from fastapi import HTTPException, Response

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
    """Run main.login with the `user` SELECT stubbed out to return `rows`."""
    real_query, main.query = main.query, lambda sql, args=None: rows
    try:
        return main.login(
            main.LoginRequest(username=USERNAME, password=password), Response()
        )
    finally:
        main.query = real_query


def request_with(cookies=None, headers=None):
    """The two attributes current_user/session_token read off a Starlette Request."""
    return types.SimpleNamespace(cookies=cookies or {}, headers=headers or {})


def reset():
    main.SESSIONS.clear()
    main.LOGIN_ATTEMPTS.clear()


def test_bad_salt_does_not_block_a_valid_login():
    # A Salt_Key that is not valid even-length hex used to raise out of the loop and
    # 500 the request, so the matching row behind it was never reached.
    reset()
    user = login_against([user_row("zz-not-hex", "whatever"), matching_row()])
    assert user["user_id"] == 2, user
    assert len(main.SESSIONS) == 1


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


def test_sweep_drops_only_stale_entries():
    reset()
    now = time.time()
    main.SESSIONS["live"] = {"user": {}, "expires": now + 60}
    main.SESSIONS["dead"] = {"user": {}, "expires": now - 1}
    main.LOGIN_ATTEMPTS["recent"] = [now - 1]
    main.LOGIN_ATTEMPTS["old"] = [now - main.LOGIN_WINDOW - 1]
    main.LOGIN_ATTEMPTS["empty"] = []

    main.sweep_expired(now)

    assert set(main.SESSIONS) == {"live"}, main.SESSIONS
    assert set(main.LOGIN_ATTEMPTS) == {"recent"}, main.LOGIN_ATTEMPTS


def test_expiry_is_idempotent():
    # Two threads can both see an expired session; the loser used to hit KeyError.
    reset()
    main.SESSIONS["tok"] = {"user": {}, "expires": time.time() - 1}
    request = request_with(cookies={main.SESSION_COOKIE: "tok"})
    assert main.current_user(request) is None
    assert main.current_user(request) is None


def test_login_returns_the_token_it_set_as_a_cookie():
    # Header-authenticating callers have no way to read the httpOnly cookie, so the
    # login body has to carry the same token.
    reset()
    user = login_against([matching_row()])
    assert user["token"] in main.SESSIONS, user.keys()


def test_bearer_header_authenticates():
    reset()
    main.SESSIONS["tok"] = {"user": {"user_id": 7}, "expires": time.time() + 60}
    request = request_with(headers={"authorization": "Bearer tok"})
    assert main.current_user(request) == {"user_id": 7}


def test_bearer_beats_a_stale_cookie():
    # Both transports present and disagreeing: the explicit header must win, or a
    # leftover cookie would silently pin the caller to the wrong session.
    reset()
    main.SESSIONS["fresh"] = {"user": {"user_id": 7}, "expires": time.time() + 60}
    main.SESSIONS["stale"] = {"user": {"user_id": 8}, "expires": time.time() + 60}
    request = request_with(
        cookies={main.SESSION_COOKIE: "stale"},
        headers={"authorization": "Bearer fresh"},
    )
    assert main.current_user(request) == {"user_id": 7}


def test_cookie_still_authenticates_without_a_header():
    # The header is additive; the browser path must not have moved.
    reset()
    main.SESSIONS["tok"] = {"user": {"user_id": 7}, "expires": time.time() + 60}
    assert main.current_user(request_with(cookies={main.SESSION_COOKIE: "tok"})) == {
        "user_id": 7
    }


def test_malformed_authorization_headers_fall_through():
    reset()
    main.SESSIONS["tok"] = {"user": {"user_id": 7}, "expires": time.time() + 60}
    for header in ("", "Bearer", "Bearer   ", "Basic tok", "tok"):
        request = request_with(
            cookies={main.SESSION_COOKIE: "tok"}, headers={"authorization": header}
        )
        # Nothing usable in the header, so the cookie is what answers.
        assert main.current_user(request) == {"user_id": 7}, header
        assert main.current_user(request_with(headers={"authorization": header})) is None


def test_logout_drops_a_header_session():
    reset()
    main.SESSIONS["tok"] = {"user": {"user_id": 7}, "expires": time.time() + 60}
    main.logout(request_with(headers={"authorization": "Bearer tok"}), Response())
    assert "tok" not in main.SESSIONS


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
