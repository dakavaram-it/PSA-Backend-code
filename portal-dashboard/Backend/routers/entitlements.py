# Backend/routers/entitlements.py — Entitlement Management's writes:
# Create Entitlement (the `entitlement` table), Create Entitlement Group
# (`group_entitlement` + `group_entitlement_relation`) and Create User Group
# (`user_groups` + `user_group_entitlement`, one level up from an entitlement
# group). None of entitlement_type, group_entitlement.description or
# user_groups.notes have a DB-level UNIQUE constraint, so all three
# availability checks below are app-level advisories only, each re-verified
# inside its own write transaction to close (not eliminate) the race.
# group_entitlement.description and user_groups.notes did carry duplicates
# from before this check existed (see queries.py's note on
# GROUP_ENTITLEMENT_ALL_DESCRIPTIONS_SELECT) — this only blocks *new* ones
# going forward, it doesn't touch what's already there.
import re

from fastapi import APIRouter, HTTPException

from db import run, run_write_tx
from queries import (
    ENTITLEMENT_GROUP_ENTITLEMENTS_SELECT, GROUP_ENTITLEMENT_CATALOG_ENTITLEMENTS_SELECT,
    GROUP_ENTITLEMENT_USER_GROUPS_SELECT, USER_GROUP_GROUP_ENTITLEMENTS_SELECT, USER_GROUP_USERS_SELECT,
)
from schemas import EntitlementCreate, EntitlementsAssign, GroupEntitlementCreate, GroupEntitlementsAssign, UserGroupCreate
from services import (
    assign_entitlements_to_group, assign_group_entitlements, entitlement_taken,
    group_entitlement_description_taken, insert_entitlement, insert_group_entitlement, insert_user_group,
    require_group_entitlement, require_user_group, revoke_group_entitlement, user_group_notes_taken,
)

router = APIRouter(prefix="/api/portal", tags=["portal"])

# Matches the live catalog's naming convention (CONSTITUENCY_PAGE,
# PARTY_PERFORMANCE_REPORT, ...) — capital letters/digits, single underscores
# between words, no leading/trailing/doubled underscore. The frontend already
# forces this shape as the admin types; this is the server-side floor under
# that, since nothing stops a direct API call from skipping it. Shared by all
# three Create endpoints below, not just entitlement_type — Create Entitlement
# Group's description and Create User Group's notes follow the same
# convention now.
CATALOG_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


@router.get("/entitlements/availability")
def entitlement_availability(name: str):
    name = name.strip().upper()
    if not name:
        return {"available": False}
    taken = run("SELECT 1 FROM entitlement WHERE entitlement_type=%s LIMIT 1", (name,), one=True) is not None
    return {"available": not taken}


@router.post("/entitlements", status_code=201)
def create_entitlement(body: EntitlementCreate):
    name = body.entitlement_type.strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="entitlement name is required")
    if not CATALOG_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="entitlement name must be capital letters, digits and underscores only "
                   "(e.g. CAMPAIGN_DETAILS_UPDATE), with no leading, trailing or doubled underscore",
        )

    def _create(cur):
        if entitlement_taken(cur, name):
            raise HTTPException(status_code=409, detail="an entitlement with that name already exists")
        entitlement_id = insert_entitlement(cur, name)
        return {"entitlement_id": entitlement_id, "entitlement_type": name}

    return run_write_tx(_create)


@router.get("/group-entitlements/availability")
def group_entitlement_availability(name: str):
    name = name.strip().upper()
    if not name:
        return {"available": False}
    taken = run("SELECT 1 FROM group_entitlement WHERE description=%s LIMIT 1", (name,), one=True) is not None
    return {"available": not taken}


@router.post("/group-entitlements", status_code=201)
def create_group_entitlement(body: GroupEntitlementCreate):
    description = body.description.strip().upper()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    if not CATALOG_NAME_RE.fullmatch(description):
        raise HTTPException(
            status_code=400,
            detail="description must be capital letters, digits and underscores only "
                   "(e.g. CAMPAIGN_DETAILS_UPDATE_GROUP), with no leading, trailing or doubled underscore",
        )
    if len(description) > 250:
        raise HTTPException(status_code=400, detail="description must be 250 characters or fewer")
    # A group with nothing in it grants nothing — same "at least one" rule
    # component grants and access scopes follow elsewhere in this app.
    if not body.entitlement_ids:
        raise HTTPException(status_code=400, detail="select at least one entitlement")

    def _create(cur):
        if group_entitlement_description_taken(cur, description):
            raise HTTPException(status_code=409, detail="an entitlement group with that description already exists")
        group_id = insert_group_entitlement(cur, description, body.entitlement_ids)
        return {
            "group_entitlement_id": group_id,
            "description": description,
            "entitlement_ids": body.entitlement_ids,
        }

    return run_write_tx(_create)


# What a group_entitlement actually bundles — catalog-only, not filtered
# through team/user reachability the way GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT
# is. Backs a "view entitlements" expand wherever a group_entitlement shows
# up as a pickable row (Portal User Detail's "Add entitlement groups",
# Entitlement Management's own pickers), lazy-fetched per row rather than
# joined into the catalog list up front.
@router.get("/group-entitlements/{group_entitlement_id}/entitlements")
def group_entitlement_entitlements(group_entitlement_id: int):
    return run(GROUP_ENTITLEMENT_CATALOG_ENTITLEMENTS_SELECT, (group_entitlement_id,))


# The reverse of the endpoint above — which group_entitlement(s) a given
# entitlement is already bundled into. Backs Create Entitlement Group's own
# "View entitlements" checklist: an eye icon on each entitlement row shows
# what it already belongs to before an admin adds it to another group.
@router.get("/entitlements/{entitlement_id}/group-entitlements")
def entitlement_group_entitlements(entitlement_id: int):
    return run(ENTITLEMENT_GROUP_ENTITLEMENTS_SELECT, (entitlement_id,))


# Create Entitlement's "add to an existing entitlement group" option — the
# "attach more" counterpart to Create Entitlement Group's second write, for a
# group_entitlement that already exists. Mirrors
# assign_group_entitlements_to_user_group one level up.
@router.post("/group-entitlements/{group_entitlement_id}/entitlements", status_code=201)
def assign_entitlements_to_group_entitlement(group_entitlement_id: int, body: EntitlementsAssign):
    if not body.entitlement_ids:
        raise HTTPException(status_code=400, detail="select at least one entitlement")

    def _assign(cur):
        require_group_entitlement(cur, group_entitlement_id)
        new_ids = assign_entitlements_to_group(cur, group_entitlement_id, body.entitlement_ids)
        return {"group_entitlement_id": group_entitlement_id, "assigned_entitlement_ids": new_ids}

    return run_write_tx(_assign)


# Assign user groups to an entitlement group's picker — which user_groups
# already carry this group_entitlement, so the frontend can mark them
# (orange, non-pickable) instead of letting an admin re-attach one that's
# already there. Mirrors GET /users/{user_id}/entitlements's role for the
# other direction's picker.
@router.get("/group-entitlements/{group_entitlement_id}/user-groups")
def group_entitlement_user_groups(group_entitlement_id: int):
    return run(GROUP_ENTITLEMENT_USER_GROUPS_SELECT, (group_entitlement_id,))


@router.get("/user-groups/availability")
def user_group_availability(name: str):
    name = name.strip().upper()
    if not name:
        return {"available": False}
    taken = run("SELECT 1 FROM user_groups WHERE notes=%s LIMIT 1", (name,), one=True) is not None
    return {"available": not taken}


@router.post("/user-groups", status_code=201)
def create_user_group(body: UserGroupCreate):
    notes = body.notes.strip().upper()
    if not notes:
        raise HTTPException(status_code=400, detail="notes is required")
    if not CATALOG_NAME_RE.fullmatch(notes):
        raise HTTPException(
            status_code=400,
            detail="notes must be capital letters, digits and underscores only "
                   "(e.g. CAMPAIGN_MANAGERS), with no leading, trailing or doubled underscore",
        )
    if len(notes) > 250:
        raise HTTPException(status_code=400, detail="notes must be 250 characters or fewer")
    # Unlike Create Entitlement Group (an entitlement group with nothing in
    # it grants nothing, so that one still requires at least one), a user
    # group with zero entitlement groups is a legitimate empty shell — the
    # entitlement groups can be attached later via "Assign entitlement
    # groups to user group", so this isn't required up front.

    def _create(cur):
        if user_group_notes_taken(cur, notes):
            raise HTTPException(status_code=409, detail="a user group with those notes already exists")
        user_group_id = insert_user_group(cur, notes, body.group_entitlement_ids)
        return {
            "user_group_id": user_group_id,
            "notes": notes,
            "group_entitlement_ids": body.group_entitlement_ids,
        }

    return run_write_tx(_create)


# Assign entitlement groups to a user group's picker — the reverse of
# group_entitlement_user_groups above: which group_entitlements this
# user_group already carries, so the frontend can mark them (orange) the same
# way, and unchecking one calls the DELETE below to remove it.
@router.get("/user-groups/{user_group_id}/group-entitlements")
def user_group_group_entitlements(user_group_id: int):
    return run(USER_GROUP_GROUP_ENTITLEMENTS_SELECT, (user_group_id,))


# "Assign User Groups to User"'s reverse picker (pick one user_group,
# multi-select users) — which users already belong to this user_group, so the
# frontend can mark them (orange) the same way group_entitlement_user_groups
# does for its own picker. Unchecking one calls DELETE
# /users/{user_id}/groups/{group_id} (routers/users.py) to remove it, same
# endpoint the Assign User to User Groups card's own removal already uses.
@router.get("/user-groups/{user_group_id}/users")
def user_group_users(user_group_id: int):
    return run(USER_GROUP_USERS_SELECT, (user_group_id,))



# Assign entitlement groups to a user group — the "attach more" counterpart
# to Create User Group's second write, for a user_group that already exists.
# The reverse card (assign user groups to one entitlement group) calls this
# same endpoint once per selected user_group, each time with a single-item
# group_entitlement_ids list — see services.assign_group_entitlements for why
# that's a loop rather than a second, symmetric endpoint.
@router.post("/user-groups/{user_group_id}/group-entitlements", status_code=201)
def assign_group_entitlements_to_user_group(user_group_id: int, body: GroupEntitlementsAssign):
    if not body.group_entitlement_ids:
        raise HTTPException(status_code=400, detail="select at least one entitlement group")

    def _assign(cur):
        require_user_group(cur, user_group_id)
        new_ids = assign_group_entitlements(cur, user_group_id, body.group_entitlement_ids)
        return {"user_group_id": user_group_id, "assigned_group_entitlement_ids": new_ids}

    return run_write_tx(_assign)


# Both Assign directions' picker delete action — an admin unchecking an
# already-attached row in either "Assign user groups to entitlement group" or
# "Assign entitlement groups to user group" calls this same pair either way,
# since both cards read and write the one user_group_entitlement join. Same
# "no body, the two ids in the path fully identify the row" shape
# remove_user_group (routers/users.py) uses for the equivalent user-group
# picker.
@router.delete("/user-groups/{user_group_id}/group-entitlements/{group_entitlement_id}")
def remove_group_entitlement_from_user_group(user_group_id: int, group_entitlement_id: int):
    def _remove(cur):
        revoke_group_entitlement(cur, user_group_id, group_entitlement_id)
        return {"user_group_id": user_group_id, "group_entitlement_id": group_entitlement_id}

    return run_write_tx(_remove)
