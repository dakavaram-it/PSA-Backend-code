# Backend/services.py — the shared logic behind the member endpoints.
#
# Every helper here runs on a cursor the caller already owns, so a whole
# request (existence check + writes + post-write re-read) costs one pooled
# connection rather than one per statement.
from datetime import datetime

from fastapi import HTTPException

from queries import GROUP_BY, MEMBER_LOCATIONS_QUERY, MEMBER_SELECT


# --- shaping ---------------------------------------------------------------

def shape(r):
    """component_ids comes back as a comma string; the UI wants an int array."""
    ids = r.get("component_ids")
    r["component_ids"] = [int(x) for x in ids.split(",")] if ids else []
    return r


def attach_locations(cur, members_by_id):
    """members_by_id: {activity_member_id: member_dict}. Adds a `locations`
    list to each, in place. Runs on the caller's cursor so it shares the
    request's single connection."""
    ids = list(members_by_id.keys())
    for m in members_by_id.values():
        m["locations"] = []
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute(MEMBER_LOCATIONS_QUERY + f" AND AMAL.activity_member_id IN ({placeholders})", ids)
    for r in cur.fetchall():
        members_by_id[r["activity_member_id"]]["locations"].append({
            "level_id": r["level_id"], "level_name": r["level_name"],
            "location_value": r["location_value"], "location_name": r["location_name"],
        })


def require_member(cur, member_id):
    """404 if the login doesn't exist. A point lookup is enough — no need to
    run the full MEMBER_SELECT join just for this."""
    cur.execute("SELECT 1 FROM activity_member WHERE activity_member_id=%s", (member_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="not found")


def member_row(cur, member_id, with_locations=False):
    """The post-write row every write endpoint returns. Read on the writing
    connection, before commit, so it always reflects the write just made."""
    cur.execute(MEMBER_SELECT + " WHERE AM.activity_member_id = %s" + GROUP_BY, (member_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row = shape(row)
    if with_locations:
        attach_locations(cur, {row["activity_member_id"]: row})
    return row


# --- one field of a login's access each -------------------------------------
# The single-field endpoints call one of these; PUT /api/members calls
# whichever the Detail screen actually changed, all in one transaction.


def apply_role(cur, member_id, user_type_id):
    """Deactivate any active role grant, then reactivate a matching prior grant
    or insert a fresh one."""
    cur.execute(
        "UPDATE activity_member_access_type SET is_active='N' "
        "WHERE activity_member_id=%s AND is_active='Y'",
        (member_id,),
    )
    cur.execute(
        "SELECT activity_member_access_type_id FROM activity_member_access_type "
        "WHERE activity_member_id=%s AND user_type_id=%s LIMIT 1",
        (member_id, user_type_id),
    )
    existing = cur.fetchone()
    if existing:
        cur.execute(
            "UPDATE activity_member_access_type SET is_active='Y' WHERE activity_member_access_type_id=%s",
            (existing["activity_member_access_type_id"],),
        )
    else:
        cur.execute(
            "INSERT INTO activity_member_access_type (activity_member_id, user_type_id, is_active) "
            "VALUES (%s, %s, 'Y')",
            (member_id, user_type_id),
        )


def apply_active(cur, member_id, is_active):
    """Flip activity_member.is_acitve. Deactivating also kills any live OTPs."""
    cur.execute("SELECT tdp_cadre_id FROM activity_member WHERE activity_member_id=%s", (member_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    cur.execute("UPDATE activity_member SET is_acitve=%s WHERE activity_member_id=%s", (is_active, member_id))
    if is_active == "N" and row["tdp_cadre_id"]:
        cur.execute(
            "UPDATE login_otp_details SET is_valid='N', updated_time=NOW() "
            "WHERE tdp_cadre_id=%s AND is_valid='Y'",
            (row["tdp_cadre_id"],),
        )


def apply_locations(cur, member_id, locations):
    """Replace the login's whole active access_level set. location_value is
    compared with <=> (NULL-safe equals) since a level like STATE may
    legitimately carry no location_value.

    Looks the requested pairs up in one round trip rather than one SELECT plus
    one UPDATE per location — every statement here is a ~220 ms round trip to a
    remote DB, so the statement count is what this costs, not the row count."""
    cur.execute(
        "UPDATE activity_member_access_level SET is_active='N' "
        "WHERE activity_member_id=%s AND is_active='Y'",
        (member_id,),
    )
    if not locations:
        return

    match = " OR ".join(["(activity_member_level_id=%s AND activity_location_value <=> %s)"] * len(locations))
    args = [member_id]
    for loc in locations:
        args += [loc.user_level_id, loc.location_value]
    cur.execute(
        f"SELECT activity_member_access_level_id, activity_member_level_id, activity_location_value "
        f"FROM activity_member_access_level WHERE activity_member_id=%s AND ({match})",
        args,
    )
    known = {(r["activity_member_level_id"], r["activity_location_value"]):
             r["activity_member_access_level_id"] for r in cur.fetchall()}

    to_reactivate, to_insert = [], []
    for loc in locations:
        row_id = known.get((loc.user_level_id, loc.location_value))
        if row_id is None:
            to_insert.append((member_id, loc.user_level_id, loc.location_value))
        else:
            to_reactivate.append(row_id)

    if to_reactivate:
        ph = ",".join(["%s"] * len(to_reactivate))
        cur.execute(
            f"UPDATE activity_member_access_level SET is_active='Y' "
            f"WHERE activity_member_access_level_id IN ({ph})",
            to_reactivate,
        )
    if to_insert:
        cur.executemany(
            "INSERT INTO activity_member_access_level "
            "(activity_member_id, activity_member_level_id, activity_location_value, is_active) "
            "VALUES (%s, %s, %s, 'Y')",
            to_insert,
        )


def grant_components(cur, member_id, component_ids):
    """Reactivate-or-insert each component grant. Reactivating a prior (revoked)
    grant rather than inserting a duplicate row is the same pattern the
    role/level helpers use."""
    if not component_ids:
        return
    placeholders = ",".join(["%s"] * len(component_ids))
    cur.execute(
        f"SELECT component_id, activity_member_component_id FROM activity_member_component "
        f"WHERE activity_member_id=%s AND component_id IN ({placeholders})",
        [member_id, *component_ids],
    )
    known = {r["component_id"]: r["activity_member_component_id"] for r in cur.fetchall()}

    to_reactivate = [known[c] for c in component_ids if c in known]
    to_insert = [(member_id, c) for c in component_ids if c not in known]
    if to_reactivate:
        ph = ",".join(["%s"] * len(to_reactivate))
        cur.execute(
            f"UPDATE activity_member_component SET is_valid='Y' "
            f"WHERE activity_member_component_id IN ({ph}) AND is_valid<>'Y'",
            to_reactivate,
        )
    if to_insert:
        cur.executemany(
            "INSERT INTO activity_member_component (activity_member_id, component_id, is_valid) "
            "VALUES (%s, %s, 'Y')",
            to_insert,
        )


def revoke_components(cur, member_id, component_ids):
    """Soft-revoke only — flips is_valid='N', mirroring every other grant
    table's delete semantics — so re-adding later reactivates the same row
    instead of accumulating duplicates."""
    if not component_ids:
        return
    placeholders = ",".join(["%s"] * len(component_ids))
    cur.execute(
        f"UPDATE activity_member_component SET is_valid='N' "
        f"WHERE activity_member_id=%s AND component_id IN ({placeholders}) AND is_valid='Y'",
        [member_id, *component_ids],
    )


def apply_components(cur, member_id, component_ids):
    """Diff the login's active personal component grants against the desired set."""
    cur.execute(
        "SELECT component_id FROM activity_member_component "
        "WHERE activity_member_id=%s AND is_valid='Y'",
        (member_id,),
    )
    current = {r["component_id"] for r in cur.fetchall()}
    wanted = set(component_ids)
    grant_components(cur, member_id, sorted(wanted - current))
    revoke_components(cur, member_id, sorted(current - wanted))


# --- OTP -------------------------------------------------------------------
# A login's OTP lives in login_otp_details keyed off tdp_cadre_id, not
# activity_member_id, so both OTP endpoints resolve that first. expires_at is
# read straight off updated_time (see OTP_DEFAULT_VALID_MINUTES in config.py
# for why that column holds it).

NO_OTP = {"otp": None, "generated_time": None, "expires_at": None, "is_valid": "N", "is_expired": True}


def otp_status(cur, tdp_cadre_id):
    cur.execute(
        "SELECT otp, generated_time, updated_time, is_valid FROM login_otp_details "
        "WHERE tdp_cadre_id=%s ORDER BY CASE WHEN is_valid='Y' THEN 0 ELSE 1 END, "
        "generated_time DESC LIMIT 1",
        (tdp_cadre_id,),
    )
    row = cur.fetchone()
    if not row:
        return dict(NO_OTP)
    expires_at = row["updated_time"]
    return {
        "otp": row["otp"],
        "generated_time": row["generated_time"],
        "expires_at": expires_at,
        "is_valid": row["is_valid"],
        "is_expired": row["is_valid"] != "Y" or expires_at is None or datetime.now() > expires_at,
    }
