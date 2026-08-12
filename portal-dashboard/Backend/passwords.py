# Backend/passwords.py — password hashing for newly created `user` rows.
#
# Existing rows' Hash_Key/Salt_Key were produced by a separate (Java) app, and
# that scheme can't be reproduced from the data alone — Salt_Key on real rows
# decodes to a byte array's default toString() (e.g. "[B@287f997f"), not the
# actual salt bytes, so the true inputs to those rows' hash were never
# persisted anywhere recoverable.
#
# New rows created here use their own scheme instead, per product spec:
# MD5-prehash the password, then derive a key via PBKDF2-HMAC-SHA256 over that
# digest with a fresh random salt. Both Hash_Key and Salt_Key are stored
# hex-encoded (a real, reversible encoding — unlike the legacy rows' Salt_Key).
import hashlib
import secrets

ITERATIONS = 200_000
SALT_BYTES = 16
KEY_LENGTH = 64  # matches the byte length observed on existing Hash_Key values


def hash_password(password):
    """Returns (hash_key_hex, salt_key_hex) for a new `user` row."""
    salt = secrets.token_bytes(SALT_BYTES)
    md5_digest = hashlib.md5(password.encode("utf-8")).hexdigest().encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", md5_digest, salt, ITERATIONS, dklen=KEY_LENGTH)
    return derived.hex(), salt.hex()
