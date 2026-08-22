"""S3 file storage for the monthly-activity Update modals (Pedala Sevalo,
Swatch Andhra, Pattadar Passbook) — the one place in this service that
persists an uploaded file rather than leaving it session-only in the
browser, the posture every other upload in the app still has.

Same account/bucket the portal's own `uploadNominationFile` uses
(`portal-frontend-code/Backend/main.py`): `put_object` on upload, a fresh
5-minute `generate_presigned_url` per view rather than a stored public link
— the bucket blocks public access, so `file_path` alone is never fetchable
and a leaked URL cannot be replayed past its expiry.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import config

# `None` until `S3_ACCESS_KEY`/`S3_SECRET_KEY` are set — every function below
# checks this first and answers 503 rather than raising at import time, so
# the rest of the service runs fine before the credentials exist.
CLIENT = None
if config.S3_ACCESS_KEY and config.S3_SECRET_KEY:
    CLIENT = boto3.client(
        "s3",
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
        region_name=config.S3_REGION,
    )

# Accepted the same way every other upload field in this app's frontend
# already restricts its own file picker (`accept="image/*,.pdf,application/pdf"`
# in AddActivityEntryModal, AddMeetingEntryModal before it) — not the portal's
# PDF-only nomination rule, which is a different document type entirely.
ALLOWED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class StorageUnavailable(Exception):
    """Raised by `upload`/`presigned_url` when no client is configured."""


class UploadFailed(Exception):
    """Raised by `upload` when S3 itself rejects or drops the request."""


def _extension(content_type: str, filename: str | None) -> str:
    guess = mimetypes.guess_extension(content_type) if filename else None
    return ALLOWED_CONTENT_TYPES.get(content_type) or guess or ""


def upload(*, folder: str, content: bytes, content_type: str, filename: str | None = None) -> str:
    """Puts one file under `folder/DDMMYY/<uuid4><ext>` and returns the
    `bucket/key` path to store in a `file_path` column — mirrors the
    portal's own `election_nominations/...` key shape, `folder` standing in
    for that fixed prefix so each caller gets its own namespace in the same
    bucket."""
    if CLIENT is None:
        raise StorageUnavailable
    ext = _extension(content_type, filename)
    key = f"{folder}/{datetime.now().strftime('%d%m%y')}/{uuid4()}{ext}"
    try:
        CLIENT.put_object(Bucket=config.S3_BUCKET, Key=key, Body=content, ContentType=content_type)
    except (BotoCoreError, ClientError) as err:
        raise UploadFailed from err
    return f"{config.S3_BUCKET}/{key}"


def presigned_url(file_path: str, expires_in: int = 300) -> str:
    """A short-lived GET link for a `bucket/key` path saved by `upload`."""
    if CLIENT is None:
        raise StorageUnavailable
    bucket, key = file_path.split("/", 1)
    try:
        return CLIENT.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
        )
    except (BotoCoreError, ClientError) as err:
        raise UploadFailed from err
