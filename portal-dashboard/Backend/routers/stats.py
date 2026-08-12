# Backend/routers/stats.py — KPI counts off the `user` table
# (dakavara_pa's portal login accounts — distinct from the admin dashboard's
# activity_member/tdp_cadre schema). Read-only.
from fastapi import APIRouter

from db import run
from queries import (
    ENTITLEMENT_STATS_SELECT, ENTITLEMENT_TOTAL_SELECT,
    GROUP_ENTITLEMENT_ALL_DESCRIPTIONS_SELECT, GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT,
    TEAM_STATS_SELECT, TEAM_USER_SEARCH_COLUMN, TEAM_USER_SEARCH_STATUS_FILTER, TEAM_USER_STATUS_FILTER,
    TEAM_USERS_SELECT, USER_STATS_SELECT,
)

router = APIRouter(prefix="/api/portal", tags=["portal"])


@router.get("/users/stats")
def user_stats():
    row = run(USER_STATS_SELECT, one=True)
    return {"total": int(row["total"]), "active": int(row["active"] or 0), "inactive": int(row["inactive"] or 0)}


@router.get("/teams/stats")
def team_stats():
    rows = run(TEAM_STATS_SELECT)
    return [{"id": r["id"], "name": r["name"], "logins": int(r["logins"])} for r in rows]


@router.get("/entitlements/stats")
def entitlement_stats():
    rows = run(ENTITLEMENT_STATS_SELECT)
    return [
        {"id": r["entitlement_id"], "type": r["entitlement_type"], "user_count": int(r["user_count"])}
        for r in rows
    ]


@router.get("/entitlements/total")
def entitlement_total():
    row = run(ENTITLEMENT_TOTAL_SELECT, one=True)
    return {"total": int(row["total_entitlement"])}


@router.get("/group-entitlements/entitlements")
def group_entitlement_entitlements():
    return run(GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT)


# The Group Entitlements "View All" page's own list — every unique
# description in the catalog, entitlement_count 0 included, so a group
# nobody with a team currently reaches any entitlement of still gets a row
# (the "No entitlements found." case). Counts are derived from
# GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT's own rows rather than a second,
# differently-shaped query, so this list's counts can never drift from what
# /group-entitlements/entitlements actually returns for the same name.
@router.get("/group-entitlements/by-description")
def group_entitlement_descriptions():
    all_names = [r["description"] for r in run(GROUP_ENTITLEMENT_ALL_DESCRIPTIONS_SELECT)]
    types_by_description = {}
    for r in run(GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT):
        types_by_description.setdefault(r["group_entitlement_description"], set()).add(r["entitlement_type"])
    result = [
        {"description": name, "entitlement_count": len(types_by_description.get(name, ()))}
        for name in all_names
    ]
    result.sort(key=lambda row: (-row["entitlement_count"], row["description"] or ""))
    return result


@router.get("/users")
def users(status: str = "active"):
    return run(TEAM_USERS_SELECT + TEAM_USER_STATUS_FILTER.get(status, TEAM_USER_STATUS_FILTER["active"]))


# Find a User (Portal Dashboard). field is whitelisted against
# TEAM_USER_SEARCH_COLUMN before it ever reaches the query string — q is the
# only untrusted value, and it goes in as a bind parameter.
@router.get("/users/search")
def search_users(field: str, q: str, status: str = "all"):
    column = TEAM_USER_SEARCH_COLUMN.get(field)
    if not column or not q.strip():
        return []
    status_sql = TEAM_USER_SEARCH_STATUS_FILTER.get(status, "")
    sql = f"{TEAM_USERS_SELECT} WHERE {column} LIKE %s{status_sql} ORDER BY u.user_id LIMIT 50"
    return run(sql, (f"%{q.strip()}%",))
