# Backend/routers/lookups.py — the reference lists the Detail
# screen's access-value picker needs, one per access_type. All read-only.
from fastapi import APIRouter

from db import run
from queries import (
    CONSTITUENCIES_SELECT, DISTRICTS_SELECT, ENTITLEMENTS_SELECT, GROUP_ENTITLEMENTS_SELECT,
    PARLIAMENTS_SELECT, STATES_SELECT, USER_GROUPS_SELECT, ZONES_SELECT,
)

router = APIRouter(prefix="/api/portal/lookups", tags=["portal"])


@router.get("/constituencies")
def constituencies():
    return run(CONSTITUENCIES_SELECT)


# The whole entitlement catalog — Create Entitlement Group's "view
# entitlements" checklist (Entitlement Management).
@router.get("/entitlements")
def entitlements():
    return run(ENTITLEMENTS_SELECT)


# The whole group_entitlement catalog — Create User Group's "view entitlement
# groups" checklist (Entitlement Management).
@router.get("/group-entitlements")
def group_entitlements():
    return run(GROUP_ENTITLEMENTS_SELECT)


# The whole user_groups catalog — Assign User to User Groups' "view user
# groups" checklist (Entitlement Management).
@router.get("/user-groups")
def user_groups():
    return run(USER_GROUPS_SELECT)


@router.get("/parliaments")
def parliaments():
    return run(PARLIAMENTS_SELECT)


@router.get("/districts")
def districts():
    return run(DISTRICTS_SELECT)


@router.get("/zones")
def zones():
    return run(ZONES_SELECT)


@router.get("/states")
def states():
    return run(STATES_SELECT)
