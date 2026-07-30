# Backend/test_auth.py — the login check is the one place a wrong answer lets a
# stranger into the console, so it gets a runnable check.
#   ./venv/Scripts/python test_auth.py
from config import LOGIN_PASSWORD, LOGIN_USERNAME
from routers.auth import check_credentials

assert check_credentials(LOGIN_USERNAME, LOGIN_PASSWORD) is True

# Everything else is refused: wrong password, wrong username, both wrong,
# empty, whitespace-padded, case-flipped, non-ASCII (must refuse, not raise).
for user, pwd in [
    (LOGIN_USERNAME, LOGIN_PASSWORD + "x"),
    (LOGIN_USERNAME, LOGIN_PASSWORD[:-1]),
    (LOGIN_USERNAME, ""),
    (LOGIN_USERNAME.upper() + "X", LOGIN_PASSWORD),
    (LOGIN_USERNAME + " ", LOGIN_PASSWORD),
    (LOGIN_USERNAME, " " + LOGIN_PASSWORD),
    ("", ""),
    ("admin", "admin"),
    ("రవి", "పాస్"),
]:
    assert check_credentials(user, pwd) is False, f"accepted {user!r}"

print("auth ok")
