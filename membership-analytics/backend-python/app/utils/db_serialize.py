from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.utils.cadre_photo import build_cadre_photo_url


def serialize_db_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def serialize_db_row(row: dict | None) -> dict | None:
    if not row:
        return None
    return {str(key): serialize_db_value(val) for key, val in row.items()}


def attach_photo_url(row: dict | None, image_base_url: str, image_keys: tuple[str, ...] = ("IMAGE", "image", "photo")) -> dict | None:
    if not row:
        return None
    data = serialize_db_row(row) or {}
    image_path = None
    for key in image_keys:
        if data.get(key):
            image_path = data[key]
            break
    photo_url = build_cadre_photo_url(image_path, image_base_url)
    if photo_url:
        data["photoUrl"] = photo_url
    return data
