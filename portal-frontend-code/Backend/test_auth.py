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
    request = types.SimpleNamespace(cookies={main.SESSION_COOKIE: "tok"})
    assert main.current_user(request) is None
    assert main.current_user(request) is None


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
