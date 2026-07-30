# Backend/routers/auth.py — the console's single login check.
#
# This console has exactly one operator account and it is defined only in the
# repo-root .env (LOGIN_USERNAME / LOGIN_PASSWORD). There is no users table, no
# second source and no bypass: any other pair is rejected here.
#
# The check lives server-side on purpose. A Vite-time credential (VITE_*) would
# be baked into the JavaScript bundle and readable by anyone who opens the
# browser's Sources tab, so the password never leaves the backend process — the
# frontend only ever learns "yes" or "no".
from secrets import compare_digest

from fastapi import APIRouter, HTTPException

from config import LOGIN_PASSWORD, LOGIN_USERNAME
from schemas import LoginRequest

router = APIRouter(prefix="/api", tags=["auth"])


def check_credentials(username: str, password: str) -> bool:
    """True only for the exact .env pair, false for everything else.

    Both halves are compared with compare_digest and combined with a
    non-short-circuiting `&`, so a wrong username costs the same as a wrong
    password and neither leaks its length through timing. Compared as bytes
    because compare_digest rejects non-ASCII str — a typed non-ASCII character
    would otherwise raise TypeError and surface as a 500 rather than a refusal.
    """
    ok_user = compare_digest(username.encode(), LOGIN_USERNAME.encode())
    ok_pass = compare_digest(password.encode(), LOGIN_PASSWORD.encode())
    return bool(ok_user & ok_pass)


@router.post("/login")
def login(body: LoginRequest):
    # One message for both failure modes — a distinct "no such user" would let
    # an outsider enumerate the valid username.
    if not check_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"username": LOGIN_USERNAME}
