"""Remarks categories — the roster behind `ViewRemarksModal`'s Category
selector. `feedback_comment` carries no category column yet, so this is a
lookup list for the dropdown, not a filter over real remarks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import db

router = APIRouter(prefix="/api/remarks-categories", tags=["remarks"])


@router.get("")
def list_remarks_categories() -> list[dict[str, Any]]:
    rows = db.rows(
        "SELECT remarks_category_id, category_name FROM remarks_category ORDER BY remarks_category_id"
    )
    return [{"id": r["remarks_category_id"], "name": r["category_name"] or ""} for r in rows]
