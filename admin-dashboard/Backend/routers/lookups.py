# Backend/routers/lookups.py — the reference tables the UI needs to render
# roles, levels, components and the location pickers. All read-only.
from fastapi import APIRouter

from db import run

router = APIRouter(prefix="/api/lookups", tags=["lookups"])


@router.get("/user-types")
def user_types():
    return run("SELECT user_type_id AS id, type, short_name AS short, order_no FROM user_type ORDER BY user_type_id")


@router.get("/user-levels")
def user_levels():
    # used_level_ids are the only levels this console ever writes: 5 ASSEMBLY,
    # 4 PARLIAMENT, 2 STATE. The rest exist in user_level but carry no
    # location resolution (see MEMBER_SELECT's CASE) and aren't offered.
    levels = run("SELECT user_level_id AS id, level AS name FROM user_level ORDER BY user_level_id")
    return {"levels": levels, "used_level_ids": [5, 4, 2]}


@router.get("/components")
def components():
    return run(
        "SELECT component_id AS id, name, actual_name AS actual, dashboard_display_name AS display, order_no, is_active "
        "FROM component ORDER BY component_id"
    )


@router.get("/constituencies")
def constituencies():
    # Live AP assembly constituencies (175) — election_scope_id 2.
    return run(
        "SELECT * FROM constituency "
        "WHERE state_id = 1 AND deform_date IS NULL AND election_scope_id = 2 "
        "GROUP BY constituency_id"
    )


@router.get("/parliaments")
def parliaments():
    # Live AP parliamentary constituencies (25) — election_scope_id 1, as of
    # the 2024 election.
    return run(
        "SELECT C.constituency_id, C.name, C.election_scope_id "
        "FROM constituency C "
        "JOIN election E ON E.election_scope_id = C.election_scope_id "
        "WHERE C.election_scope_id = 1 AND C.state_id = 1 "
        "AND E.election_year = 2024 AND C.deform_date IS NULL"
    )
