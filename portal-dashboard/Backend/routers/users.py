# Backend/routers/users.py — the real `user` table's writes: editing an
# existing row's access_type/access_value and is_enabled (Active/Deactivate)
# from the Detail screen, plus Create New User (Portal Dashboard) — a single
# INSERT, unlike the admin dashboard's two-write cadre+member flow, because a `user`
# row has no separate identity table behind it.
import re
from datetime import date, datetime

from fastapi import APIRouter, HTTPException

from db import run, run_write_tx
from queries import USER_ENTITLEMENT_MENU_SELECT, USER_ENTITLEMENTS_SELECT
from schemas import UserCreate, UserGroupsAssign, UserSave
from services import (
    apply_access, apply_active, assign_user_groups, insert_user, require_user, revoke_user_group,
    user_row, username_taken,
)

router = APIRouter(prefix="/api/portal", tags=["portal"])

ACCESS_TYPES = {"MLA", "MP", "DISTRICT", "ZONE", "STATE"}
GENDERS = {"Male", "Female", "Other"}
# Admin-entered, not generated — letters and digits only, in any mix (all
# digits and all letters both allowed; no symbols).
PASSWORD_RE = re.compile(r"[A-Za-z0-9]+")


def _check_password(password):
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    if not PASSWORD_RE.fullmatch(password):
        raise HTTPException(status_code=400, detail="password must contain only letters and digits")


@router.put("/users/{user_id}")
def save_user(user_id: int, body: UserSave):
    # access_value's meaning depends on access_type (a constituency id under
    # MLA/MP means something different than the same id under DISTRICT) — the
    # picker on the frontend always sends both together, so one without the
    # other means a caller bypassed it rather than a legitimate partial edit.
    if (body.access_type is None) != (body.access_value is None):
        raise HTTPException(status_code=400, detail="access_type and access_value must be set together")
    if body.is_enabled is not None and body.is_enabled not in ("Y", "N"):
        raise HTTPException(status_code=400, detail="is_enabled must be 'Y' or 'N'")
    if body.password is not None:
        _check_password(body.password)
        # RE-GUARDED: this was briefly wired to services.apply_password, which
        # genuinely overwrites Hash_Key/Salt_Key with this console's own
        # scheme. A live test confirmed the predicted failure — the reset
        # account then got "invalid username or password" logging in, so
        # something else really does verify these credentials in the
        # original (unrecoverable) format, and this console's scheme doesn't
        # match it. That account's original Hash_Key/Salt_Key is already
        # gone (no history table); this guard only stops it happening to
        # anyone else. Do not re-enable by calling apply_password here until
        # the real verifying scheme is known and used instead.
        raise HTTPException(
            status_code=501,
            detail=(
                "Password reset is disabled — a live test confirmed it breaks login on the "
                "system that actually authenticates these accounts. Do not re-enable without "
                "using that system's real hashing scheme."
            ),
        )

    def _save(cur):
        require_user(cur, user_id)
        if body.access_type is not None:
            apply_access(cur, user_id, body.access_type, body.access_value)
        if body.is_enabled is not None:
            apply_active(cur, user_id, body.is_enabled)
        return user_row(cur, user_id)

    return run_write_tx(_save)


# Portal User Detail's "Groups & Entitlements" card — every user_group this
# user belongs to, and every entitlement each of those groups carries. Not
# filtered by is_enabled: the grant rows are the same whether the account is
# active or not, so this reads identically for both — the screen shows
# Active/Inactive separately, from user_row.
@router.get("/users/{user_id}/entitlements")
def user_entitlements(user_id: int):
    return run(USER_ENTITLEMENTS_SELECT, (user_id,))


# The sidebar menu's source list — one row per distinct entitlement this user
# reaches, deduped across whichever user_group/group_entitlement path grants
# it. Separate from user_entitlements above: that endpoint's grid is the
# Detail screen's per-group breakdown, this one is just "which menu items does
# this login get."
@router.get("/users/{user_id}/entitlements/menu")
def user_entitlement_menu(user_id: int):
    return run(USER_ENTITLEMENT_MENU_SELECT, (user_id,))


# Groups & Entitlements card's delete action — revokes this one user from one
# group (see services.revoke_user_group for why "delete this entitlement" can
# only ever mean "remove the group that grants it": there's no per-user row
# anywhere below user_group_relation to delete more narrowly). Returns the
# user's refreshed entitlement list in the same transaction, same "write
# returns the new state" contract as PUT /users/{user_id}.
@router.delete("/users/{user_id}/groups/{group_id}")
def remove_user_group(user_id: int, group_id: int):
    def _delete(cur):
        revoke_user_group(cur, user_id, group_id)
        cur.execute(USER_ENTITLEMENTS_SELECT, (user_id,))
        return cur.fetchall()

    return run_write_tx(_delete)


# Entitlement Management's Assign User to User Groups card — the write behind
# remove_user_group's counterpart. Same "returns the refreshed entitlement
# list" contract, so the card can show what actually landed for that user.
@router.post("/users/{user_id}/groups", status_code=201)
def add_user_groups(user_id: int, body: UserGroupsAssign):
    if not body.group_ids:
        raise HTTPException(status_code=400, detail="select at least one user group")

    def _assign(cur):
        require_user(cur, user_id)
        assign_user_groups(cur, user_id, body.group_ids)
        cur.execute(USER_ENTITLEMENTS_SELECT, (user_id,))
        return cur.fetchall()

    return run_write_tx(_assign)


# Create New User's username field checks this on every keystroke (debounced
# client-side) — username has no DB-level UNIQUE constraint (it's a plain
# index, not a key), so this is an app-level check only, re-verified inside
# the transaction below to close (not eliminate) the race.
@router.get("/users/username-available")
def username_available(username: str):
    username = username.strip()
    if not username:
        return {"available": False}
    taken = run("SELECT 1 FROM user WHERE username=%s LIMIT 1", (username,), one=True) is not None
    return {"available": not taken}


@router.post("/users", status_code=201)
def create_user(body: UserCreate):
    # ON HOLD: insert_user's Hash_Key/Salt_Key come from the same
    # now-confirmed-incompatible scheme apply_password uses (see that
    # function's docstring for the live test that broke a password reset).
    # Create New User was never itself tested, but there's no reason to
    # believe a freshly created account would authenticate any better than a
    # reset one against whatever really verifies these logins — so creation
    # is paused here too until the real scheme is known. Validation below is
    # left intact (unreachable while this raises) rather than deleted, so
    # re-enabling is just removing this block once that's resolved.
    raise HTTPException(
        status_code=501,
        detail=(
            "Create New User is on hold — the password/credential scheme this console "
            "writes is confirmed incompatible with whatever actually authenticates these "
            "accounts (see the password reset incident). Not safe to create accounts until "
            "the real hashing scheme is known."
        ),
    )

    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    if not body.password:
        raise HTTPException(status_code=400, detail="password is required")
    _check_password(body.password)
    if not body.firstname.strip():
        raise HTTPException(status_code=400, detail="firstname is required")
    if not body.lastname.strip():
        raise HTTPException(status_code=400, detail="lastname is required")
    if body.gender not in GENDERS:
        raise HTTPException(status_code=400, detail=f"gender must be one of {', '.join(sorted(GENDERS))}")
    if not re.fullmatch(r"\d{10}", body.mobile or ""):
        raise HTTPException(status_code=400, detail="mobile must be exactly 10 digits")
    if not body.address.strip():
        raise HTTPException(status_code=400, detail="address is required")
    if body.access_type not in ACCESS_TYPES:
        raise HTTPException(status_code=400, detail=f"access_type must be one of {', '.join(sorted(ACCESS_TYPES))}")
    if not body.access_value:
        raise HTTPException(status_code=400, detail="access_value is required")
    if body.is_otp_required not in ("Y", "N"):
        raise HTTPException(status_code=400, detail="is_otp_required must be 'Y' or 'N'")
    try:
        dob = datetime.strptime(body.dateofbirth, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="dateofbirth must be YYYY-MM-DD")
    if dob >= date.today():
        raise HTTPException(status_code=400, detail="dateofbirth must be in the past")

    def _create(cur):
        if username_taken(cur, username):
            raise HTTPException(status_code=409, detail="username already taken")
        user_id = insert_user(cur, body, username)
        return user_row(cur, user_id)

    return run_write_tx(_create)
