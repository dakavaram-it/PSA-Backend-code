# Backend/routers/members.py — full CRUD for a login (activity_member plus its
# access_type / access_level / component grants).
#
# There is no authentication in front of these endpoints — anyone who can reach
# the API can call the write ones. Put this behind auth before exposing it
# outside a trusted network.
import random
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from config import OTP_DEFAULT_VALID_MINUTES
from db import read_cursor, run, run_write_tx
from queries import GROUP_BY, MEMBER_SELECT, STATUS_FILTER
from schemas import (
    ActiveUpdate, ComponentGrant, LevelUpdate, MemberCreate, MemberSave,
    OtpRegenerate, RoleUpdate,
)
from services import (
    NO_OTP, apply_active, apply_components, apply_locations, apply_role,
    attach_locations, grant_components, member_row, otp_status, require_member,
    revoke_components, shape,
)

router = APIRouter(prefix="/api/members", tags=["members"])


# --- reads -----------------------------------------------------------------

# 1) member list  (?status=all|active|inactive, default active)
@router.get("")
def members(status: str = "active"):
    with read_cursor() as cur:
        rows = run(MEMBER_SELECT + STATUS_FILTER.get(status, "") + GROUP_BY, cur=cur)
        result = [shape(r) for r in rows]
        attach_locations(cur, {m["activity_member_id"]: m for m in result})
    return result


# 2) single member (any status, so a deactivated login can still be opened)
@router.get("/{member_id}")
def member(member_id: int):
    with read_cursor() as cur:
        row = run(MEMBER_SELECT + " WHERE AM.activity_member_id = %s" + GROUP_BY, (member_id,), one=True, cur=cur)
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        row = shape(row)
        attach_locations(cur, {row["activity_member_id"]: row})
    return row


# --- writes ----------------------------------------------------------------

# 3) create a login (New login → cadre found, no activity_member yet).
# A cadre only gets dashboard access once it has an activity_member row plus
# its three grant rows (role/level/components). Refuses to create a second
# login for a cadre that already has one (activity_member_id 581 is a
# reserved/placeholder record and is ignored for this check) — use the
# role/level/active endpoints to change an existing login instead.
@router.post("", status_code=201)
def create_member(body: MemberCreate):
    if not body.locations:
        raise HTTPException(status_code=400, detail="at least one location is required")

    def _create(cur):
        cur.execute(
            "SELECT first_name, last_name FROM tdp_cadre "
            "WHERE tdp_cadre_id=%s AND is_deleted='N'",
            (body.tdp_cadre_id,),
        )
        cadre_row = cur.fetchone()
        if not cadre_row:
            raise HTTPException(status_code=404, detail="no cadre for that id")

        cur.execute(
            "SELECT activity_member_id FROM activity_member "
            "WHERE tdp_cadre_id=%s AND activity_member_id <> 581",
            (body.tdp_cadre_id,),
        )
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="a login already exists for this cadre")

        # updated_by=1, state_id=1 and activity_member_enrollment_id=2 ('2016 -
        # 2018', the is_active cycle) are static for every login this console
        # creates — they match what the bulk of existing rows carry.
        member_name = f"{cadre_row['first_name'] or ''} {cadre_row['last_name'] or ''}".strip() or None
        cur.execute(
            "INSERT INTO activity_member (tdp_cadre_id, member_name, is_acitve, inserted_time, "
            "updated_by, state_id, activity_member_enrollment_id) "
            "VALUES (%s, %s, 'Y', NOW(), 1, 1, 2)",
            (body.tdp_cadre_id, member_name),
        )
        new_id = cur.lastrowid
        cur.execute(
            "INSERT INTO activity_member_access_type (activity_member_id, user_type_id, is_active) "
            "VALUES (%s, %s, 'Y')",
            (new_id, body.user_type_id),
        )
        for loc in body.locations:
            cur.execute(
                "INSERT INTO activity_member_access_level "
                "(activity_member_id, activity_member_level_id, activity_location_value, is_active) "
                "VALUES (%s, %s, %s, 'Y')",
                (new_id, loc.user_level_id, loc.location_value),
            )
        if body.component_ids:
            cur.executemany(
                "INSERT INTO activity_member_component (activity_member_id, component_id, is_valid) "
                "VALUES (%s, %s, 'Y')",
                [(new_id, component_id) for component_id in body.component_ids],
            )
        return member_row(cur, new_id)

    return run_write_tx(_create)


# 4) apply a whole Detail-screen "Save changes" in one request and one
# transaction. Only the fields present in the body are touched, so the frontend
# still sends only what actually changed; the difference from firing one
# request per field is that they now land together instead of one round trip at
# a time. Atomic as a side effect: a failure part-way no longer leaves the
# login with its new role but its old scope.
@router.put("/{member_id}")
def save_member(member_id: int, body: MemberSave):
    if body.is_active is not None and body.is_active not in ("Y", "N"):
        raise HTTPException(status_code=400, detail="is_active must be 'Y' or 'N'")

    def _save(cur):
        require_member(cur, member_id)
        if body.user_type_id is not None:
            apply_role(cur, member_id, body.user_type_id)
        if body.is_active is not None:
            apply_active(cur, member_id, body.is_active)
        if body.locations is not None:
            apply_locations(cur, member_id, body.locations)
        if body.component_ids is not None:
            apply_components(cur, member_id, body.component_ids)
        return member_row(cur, member_id, with_locations=True)

    return run_write_tx(_save)


# 5) change a login's role (New login → existing-login panel).
@router.put("/{member_id}/role")
def update_role(member_id: int, body: RoleUpdate):
    def _update(cur):
        require_member(cur, member_id)
        apply_role(cur, member_id, body.user_type_id)
        return member_row(cur, member_id)

    return run_write_tx(_update)


# 6) activate / deactivate a login. Deactivating also kills any live OTPs.
@router.put("/{member_id}/active")
def update_active(member_id: int, body: ActiveUpdate):
    if body.is_active not in ("Y", "N"):
        raise HTTPException(status_code=400, detail="is_active must be 'Y' or 'N'")

    def _update(cur):
        apply_active(cur, member_id, body.is_active)
        return member_row(cur, member_id)

    return run_write_tx(_update)


# 7) change a login's geographic scope (Detail screen). A login can hold
# several active access_level grants at once, so this replaces the whole active
# set in one transaction.
@router.put("/{member_id}/level")
def update_level(member_id: int, body: LevelUpdate):
    def _update(cur):
        require_member(cur, member_id)
        apply_locations(cur, member_id, body.locations)
        return member_row(cur, member_id, with_locations=True)

    return run_write_tx(_update)


# 8) grant a personal component to a login (Detail screen "Add component").
@router.post("/{member_id}/components")
def add_component(member_id: int, body: ComponentGrant):
    def _add(cur):
        require_member(cur, member_id)
        grant_components(cur, member_id, [body.component_id])
        return member_row(cur, member_id)

    return run_write_tx(_add)


# 9) revoke a personal component from a login (Detail screen "Remove
# component"). Soft-revoke only — flips is_valid='N'.
@router.delete("/{member_id}/components/{component_id}")
def remove_component(member_id: int, component_id: int):
    def _remove(cur):
        require_member(cur, member_id)
        revoke_components(cur, member_id, [component_id])
        return member_row(cur, member_id)

    return run_write_tx(_remove)


# 10) soft-delete a login (Detail screen). Distinct from deactivate: cascades
# is_active/is_valid='N' across every grant table (role, level, components),
# not just activity_member.is_acitve, so a later reactivate comes back with no
# stale grants rather than silently restoring the old access set.
@router.delete("/{member_id}")
def delete_member(member_id: int):
    def _delete(cur):
        cur.execute("SELECT tdp_cadre_id FROM activity_member WHERE activity_member_id=%s", (member_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")

        cur.execute("UPDATE activity_member SET is_acitve='N' WHERE activity_member_id=%s", (member_id,))
        cur.execute(
            "UPDATE activity_member_access_type SET is_active='N' WHERE activity_member_id=%s AND is_active='Y'",
            (member_id,),
        )
        cur.execute(
            "UPDATE activity_member_access_level SET is_active='N' WHERE activity_member_id=%s AND is_active='Y'",
            (member_id,),
        )
        cur.execute(
            "UPDATE activity_member_component SET is_valid='N' WHERE activity_member_id=%s AND is_valid='Y'",
            (member_id,),
        )
        if row["tdp_cadre_id"]:
            cur.execute(
                "UPDATE login_otp_details SET is_valid='N', updated_time=NOW() "
                "WHERE tdp_cadre_id=%s AND is_valid='Y'",
                (row["tdp_cadre_id"],),
            )
        return member_row(cur, member_id)

    return run_write_tx(_delete)


# --- OTP -------------------------------------------------------------------

@router.get("/{member_id}/otp")
def member_otp(member_id: int):
    with read_cursor() as cur:
        login = run("SELECT tdp_cadre_id FROM activity_member WHERE activity_member_id=%s",
                    (member_id,), one=True, cur=cur)
        if not login:
            raise HTTPException(status_code=404, detail="not found")
        if not login["tdp_cadre_id"]:
            return dict(NO_OTP)
        return otp_status(cur, login["tdp_cadre_id"])


# Invalidates any live OTP and inserts a fresh one, same invalidate-then-insert
# order as POST /api/cadre. `otp` in the body lets the frontend save a value it
# already staged for preview in the "Reset OTP" modal; omitted, the server
# generates one. `valid_till` is the admin-picked expiry from that same modal,
# stored in the new row's updated_time; omitted, it defaults to
# +OTP_DEFAULT_VALID_MINUTES.
@router.post("/{member_id}/otp/regenerate")
def regenerate_otp(member_id: int, body: OtpRegenerate):
    otp = (body.otp or "").strip()
    if otp and not re.fullmatch(r"\d{6}", otp):
        raise HTTPException(status_code=400, detail="otp must be exactly 6 digits")
    if not otp:
        otp = f"{random.randint(0, 999999):06d}"

    now = datetime.now()
    valid_till = body.valid_till
    if valid_till and valid_till <= now:
        raise HTTPException(status_code=400, detail="valid_till must be in the future")
    if not valid_till:
        valid_till = now + timedelta(minutes=OTP_DEFAULT_VALID_MINUTES)

    def _regen(cur):
        cur.execute(
            "SELECT AM.tdp_cadre_id, TC.membership_id, TC.mobile_no FROM activity_member AM "
            "LEFT JOIN tdp_cadre TC ON TC.tdp_cadre_id = AM.tdp_cadre_id "
            "WHERE AM.activity_member_id=%s",
            (member_id,),
        )
        login = cur.fetchone()
        if not login:
            raise HTTPException(status_code=404, detail="not found")
        if not login["tdp_cadre_id"]:
            raise HTTPException(status_code=409, detail="login has no linked cadre record")

        cur.execute(
            "UPDATE login_otp_details SET is_valid='N', updated_time=NOW() "
            "WHERE tdp_cadre_id=%s AND is_valid='Y'",
            (login["tdp_cadre_id"],),
        )
        cur.execute(
            "INSERT INTO login_otp_details "
            "(tdp_cadre_id, membership_id, mobile_no, otp, generated_time, updated_time, is_valid) "
            "VALUES (%s, %s, %s, %s, NOW(), %s, 'Y')",
            (login["tdp_cadre_id"], login["membership_id"], login["mobile_no"], otp, valid_till),
        )
        return otp_status(cur, login["tdp_cadre_id"])

    return run_write_tx(_regen)
