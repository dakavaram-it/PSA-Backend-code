# Backend/services.py — write-side helpers for routers/users.py.
# Mirrors ../admin-dashboard/Backend/services.py's shape (require_*, *_row, apply_*).
from fastapi import HTTPException

from passwords import hash_password
from queries import TEAM_USER_BY_ID_SELECT


def require_user(cur, user_id):
    """404 if the user doesn't exist. A point lookup is enough — no need to
    run the full TEAM_USERS_SELECT join just for this."""
    cur.execute("SELECT 1 FROM user WHERE user_id=%s", (user_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="not found")


def user_row(cur, user_id):
    """The post-write row the write endpoint returns. Read on the writing
    connection, before commit, so it always reflects the write just made."""
    cur.execute(TEAM_USER_BY_ID_SELECT, (user_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


def apply_access(cur, user_id, access_type, access_value):
    cur.execute(
        "UPDATE user SET access_type=%s, access_value=%s WHERE user_id=%s",
        (access_type, access_value, user_id),
    )


def apply_active(cur, user_id, is_enabled):
    cur.execute("UPDATE user SET is_enabled=%s WHERE user_id=%s", (is_enabled, user_id))


def revoke_user_group(cur, user_id, group_id):
    """Removes this one user from one user_group — the Groups & Entitlements
    card's delete action. This is the only per-user write available anywhere
    in the entitlement chain: user_group_relation is the single join point
    between `user` and the shared catalog (group_entitlement/
    group_entitlement_relation/entitlement), so it's the only place a change
    can be scoped to one user without touching rows other users or other
    groups also reference. Removing it drops every entitlement that group
    grants this user — there is no finer-grained table to delete a single
    entitlement out of a multi-entitlement group for just one user.

    A real DELETE, not a status flip: DESCRIBE user_group_relation shows only
    (user_group_relation_id, user_id, user_group_id) — no is_active/is_valid
    column exists here to soft-delete instead, unlike activity_member_component
    on the admin side."""
    cur.execute(
        "DELETE FROM user_group_relation WHERE user_id=%s AND user_group_id=%s",
        (user_id, group_id),
    )


def revoke_group_entitlement(cur, user_group_id, group_entitlement_id):
    """Removes one group_entitlement from one user_group — the delete side of
    assign_group_entitlements, needed once Assign user groups to entitlement
    group / Assign entitlement groups to user group (Entitlement Management)
    let an admin uncheck an already-attached row in either picker instead of
    only ever adding more. Same real-DELETE shape revoke_user_group follows —
    DESCRIBE user_group_entitlement has no is_active/is_valid column to
    soft-delete instead."""
    cur.execute(
        "DELETE FROM user_group_entitlement WHERE user_group_id=%s AND group_entitlement_id=%s",
        (user_group_id, group_entitlement_id),
    )


def require_user_group(cur, user_group_id):
    """404 if the user_group doesn't exist — same shape as require_user."""
    cur.execute("SELECT 1 FROM user_groups WHERE user_group_id=%s", (user_group_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="not found")


def assign_group_entitlements(cur, user_group_id, group_entitlement_ids):
    """Assign entitlement groups to a user group (Entitlement Management) —
    one user_group_entitlement row per selected group_entitlement the
    user_group doesn't already carry. One level up from assign_user_groups
    (that inserts user_group_relation; this inserts user_group_entitlement,
    the join insert_user_group also writes at creation time — this is the
    same table's later, "attach more" counterpart for a user_group that
    already exists). Same "skip what's already there" reasoning: no UNIQUE
    constraint on (user_group_id, group_entitlement_id) to reject a
    duplicate instead, and one would double this bundle's entitlements
    everywhere the chain is joined (Portal User Detail's Groups &
    Entitlements card, ENTITLEMENT_STATS_SELECT, ...).

    The reverse card (assign user groups to one entitlement group) reuses
    this same function looped once per selected user_group — see
    routers/entitlements.py — rather than a second, symmetric endpoint;
    same "loop, not a bulk endpoint" shape CLAUDE.md's Grant-to-a-role
    already follows."""
    cur.execute(
        "SELECT group_entitlement_id FROM user_group_entitlement WHERE user_group_id=%s",
        (user_group_id,),
    )
    have = {row["group_entitlement_id"] for row in cur.fetchall()}
    new_ids = [gid for gid in group_entitlement_ids if gid not in have]
    if new_ids:
        cur.executemany(
            "INSERT INTO user_group_entitlement (user_group_id, group_entitlement_id) VALUES (%s, %s)",
            [(user_group_id, gid) for gid in new_ids],
        )
    return new_ids


def assign_user_groups(cur, user_id, group_ids):
    """Assign User to User Groups (Entitlement Management)'s one write — one
    user_group_relation row per selected user_group the user doesn't already
    hold. Skips ids already assigned rather than inserting a duplicate: unlike
    revoke_user_group's DELETE (which removes every matching row regardless),
    a second INSERT for the same pair would leave a duplicate relation row
    behind — DESCRIBE user_group_relation has no UNIQUE constraint on
    (user_id, user_group_id) to reject it, and USER_ENTITLEMENTS_SELECT would
    then surface that group twice on the Detail screen's Groups &
    Entitlements card."""
    cur.execute("SELECT user_group_id FROM user_group_relation WHERE user_id=%s", (user_id,))
    have = {row["user_group_id"] for row in cur.fetchall()}
    new_ids = [group_id for group_id in group_ids if group_id not in have]
    if new_ids:
        cur.executemany(
            "INSERT INTO user_group_relation (user_id, user_group_id) VALUES (%s, %s)",
            [(user_id, group_id) for group_id in new_ids],
        )
    return new_ids


def apply_password(cur, user_id, password):
    """Password reset — overwrites Hash_Key/Salt_Key with this console's own
    scheme (see passwords.py), same as insert_user does on create, and resets
    is_pwd_changed to 'false' since the admin-set password hasn't been
    personalized by the user yet.

    NOT CALLED. This was briefly wired into routers/users.py's save_user, then
    disabled again after a live test: an admin reset a real user's password
    through this console, and that account then got "invalid username or
    password" logging in through whatever system actually authenticates it.
    That confirms something else checks these columns using the original
    (Java-derived, unrecoverable) format, not this one — so this scheme is
    now known-incompatible, not just unconfirmed. That account's original
    Hash_Key/Salt_Key is already gone; there is no way to restore it from
    here. Do not re-enable this without first getting the real hashing
    scheme from whatever owns that other system."""
    hash_key, salt_key = hash_password(password)
    cur.execute(
        "UPDATE user SET Hash_Key=%s, Salt_Key=%s, is_pwd_changed=%s WHERE user_id=%s",
        (hash_key, salt_key, IS_PWD_CHANGED, user_id),
    )


def username_taken(cur, username):
    cur.execute("SELECT 1 FROM user WHERE username=%s LIMIT 1", (username,))
    return cur.fetchone() is not None


def entitlement_taken(cur, entitlement_type):
    cur.execute("SELECT 1 FROM entitlement WHERE entitlement_type=%s LIMIT 1", (entitlement_type,))
    return cur.fetchone() is not None


# Newly added alongside entitlement_taken so Create Entitlement Group and
# Create User Group get the same live availability check + re-verified
# duplicate block Create Entitlement already had. Both group_entitlement.
# description and user_groups.notes still have no DB-level UNIQUE constraint
# (nothing stops a direct API call from writing a duplicate), so like
# entitlement_taken this is an app-level check only — the difference from
# before is these two now enforce it going forward, even though duplicate
# rows already exist in the live data from before this check existed.
def group_entitlement_description_taken(cur, description):
    cur.execute("SELECT 1 FROM group_entitlement WHERE description=%s LIMIT 1", (description,))
    return cur.fetchone() is not None


def user_group_notes_taken(cur, notes):
    cur.execute("SELECT 1 FROM user_groups WHERE notes=%s LIMIT 1", (notes,))
    return cur.fetchone() is not None


def require_group_entitlement(cur, group_entitlement_id):
    """404 if the group_entitlement doesn't exist — same shape as require_user_group."""
    cur.execute("SELECT 1 FROM group_entitlement WHERE group_entitlement_id=%s", (group_entitlement_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="not found")


def assign_entitlements_to_group(cur, group_entitlement_id, entitlement_ids):
    """Create Entitlement's "add to an existing entitlement group" option —
    one group_entitlement_relation row per selected entitlement the group
    doesn't already carry. One level below assign_group_entitlements (that
    inserts user_group_entitlement; this inserts group_entitlement_relation,
    the join insert_group_entitlement also writes at creation time — this is
    that table's later, "attach more" counterpart for a group_entitlement
    that already exists). Same "skip what's already there" reasoning: no
    UNIQUE constraint on (entitlement_id, group_entitlement_id) to reject a
    duplicate instead."""
    cur.execute(
        "SELECT entitlement_id FROM group_entitlement_relation WHERE group_entitlement_id=%s",
        (group_entitlement_id,),
    )
    have = {row["entitlement_id"] for row in cur.fetchall()}
    new_ids = [eid for eid in entitlement_ids if eid not in have]
    if new_ids:
        cur.executemany(
            "INSERT INTO group_entitlement_relation (entitlement_id, group_entitlement_id) VALUES (%s, %s)",
            [(eid, group_entitlement_id) for eid in new_ids],
        )
    return new_ids


def insert_entitlement(cur, entitlement_type):
    """Create Entitlement (Entitlement Management)'s one write. is_active is
    set to 'Y' explicitly — DESCRIBE entitlement shows it nullable with no
    DEFAULT, and ENTITLEMENTS_SELECT now filters WHERE is_active='Y', so a
    row left NULL would silently never appear in its own picker."""
    cur.execute("INSERT INTO entitlement (entitlement_type, is_active) VALUES (%s, 'Y')", (entitlement_type,))
    return cur.lastrowid


def insert_group_entitlement(cur, description, entitlement_ids):
    """Create Entitlement Group's one write — one group_entitlement row plus
    one group_entitlement_relation row per selected entitlement. Membership
    rows have no is_active/is_valid column (DESCRIBE group_entitlement_relation
    is just the two FKs plus its own PK — same as user_group_relation, see
    revoke_user_group's note), so that half is a plain INSERT, no
    reactivate-or-insert dance. group_entitlement itself does carry is_active
    (nullable, no DEFAULT) and GROUP_ENTITLEMENTS_SELECT now filters on it,
    same reasoning as insert_entitlement above."""
    cur.execute("INSERT INTO group_entitlement (description, is_active) VALUES (%s, 'Y')", (description,))
    group_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO group_entitlement_relation (entitlement_id, group_entitlement_id) VALUES (%s, %s)",
        [(entitlement_id, group_id) for entitlement_id in entitlement_ids],
    )
    return group_id


def insert_user_group(cur, notes, group_entitlement_ids):
    """Create User Group's one write — one user_groups row plus one
    user_group_entitlement row per selected group_entitlement (zero is valid
    — an empty shell an admin fills in later via "Assign entitlement groups
    to user group"). One level up from insert_group_entitlement: a
    user_group bundles entitlement groups, not raw entitlements —
    user_group_relation (see revoke_user_group above) is the separate, later
    step that actually grants this bundle to a user. is_active set to 'Y'
    explicitly, same reasoning as insert_entitlement — USER_GROUPS_SELECT now
    filters on it and the column has no DEFAULT."""
    cur.execute("INSERT INTO user_groups (notes, is_active) VALUES (%s, 'Y')", (notes,))
    user_group_id = cur.lastrowid
    if not group_entitlement_ids:
        return user_group_id
    cur.executemany(
        "INSERT INTO user_group_entitlement (user_group_id, group_entitlement_id) VALUES (%s, %s)",
        [(user_group_id, group_entitlement_id) for group_entitlement_id in group_entitlement_ids],
    )
    return user_group_id


# What Create New User (Portal Dashboard) stamps on every row it inserts —
# product decisions, not derived values, same spirit as the admin dashboard's
# ENROLLMENT_YEAR/ENROLLMENT_YEAR_IDS. party_id/main_account_id/user_type tie
# a console-created account to the one party/account these are all created
# under; the rest are flags every such account starts with the same way.
PARTY_ID = 872
USER_TYPE = "Politician"
MAIN_ACCOUNT_ID = 1
IS_PWD_CHANGED = "false"
LOGIN_RESTRICTION = "false"
MULTIPLE_ACCESS_RESTRICTION = "false"
PAGE_TRACKING = "N"
REQUEST_TRACKING = "N"


def insert_user(cur, body, username):
    """Create New User (Portal Dashboard)'s one write. password itself is never
    stored — only the PBKDF2 Hash_Key/Salt_Key pair `hash_password` derives
    (see passwords.py for why the legacy password/Salt_Key columns aren't
    reused). Returns the new user_id."""
    hash_key, salt_key = hash_password(body.password)
    cur.execute(
        "INSERT INTO user "
        "(username, Hash_Key, Salt_Key, firstname, lastname, gender, dateofbirth, "
        "mobile, address, access_type, access_value, is_otp_required, party_id, user_type, "
        "main_account_id, is_pwd_changed, login_restriction, multiple_access_restriction, "
        "is_enabled, page_tracking, request_tracking, registered_time) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Y',%s,%s,NOW())",
        (
            username, hash_key, salt_key, body.firstname.strip(), body.lastname.strip(),
            body.gender, body.dateofbirth, body.mobile, body.address.strip(),
            body.access_type, body.access_value, body.is_otp_required, PARTY_ID, USER_TYPE,
            MAIN_ACCOUNT_ID, IS_PWD_CHANGED, LOGIN_RESTRICTION, MULTIPLE_ACCESS_RESTRICTION,
            PAGE_TRACKING, REQUEST_TRACKING,
        ),
    )
    return cur.lastrowid
