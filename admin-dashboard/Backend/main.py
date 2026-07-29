# Backend/main.py — data layer for the UA admin console.
# Python + FastAPI + PyMySQL. Powers the frontend with real dakavara_pa data.
# Most endpoints are SELECT-only. The write endpoints below cover full CRUD
# for a login (activity_member + its access_type/access_level/component
# grants): POST /api/members creates a login (and grants) for a cadre that
# doesn't have one yet, PUT .../role, .../level and .../active update an
# existing login's role, geographic scope and active flag, POST/DELETE
# .../components grant/revoke a single personal component, and DELETE
# /api/members/{id} soft-deletes a login by cascading is_active/is_valid='N'
# across every grant table (distinct from deactivate, which only flips
# activity_member.is_acitve and leaves grants intact for a later reactivate).
# There is still no authentication in front of this API — anyone who can
# reach it can call these write endpoints. Put this behind auth before it's
# exposed outside a trusted network.
# See Backend.md for the read contract and query rationale.
#
# Run:  pip install -r requirements.txt
#       python main.py            (or: uvicorn main:app --port 4000)
import contextlib
import os
import queue
import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(dotenv_path="../.env")  # repo-root .env (git-ignored)

DB = dict(
    host=os.environ["DB_HOST"],
    port=int(os.environ["DB_PORT"]),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ["DB_NAME"],
    charset="utf8mb4",              # component labels include Telugu
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=15,
    read_timeout=60,
)

# --- connection pooling ----------------------------------------------------
# The database is a remote RDS instance, and the round trip to it is expensive:
# a fresh handshake measures ~1.1 s and the two SET SESSION pragmas another
# ~0.4 s, against ~0.2 s for an actual query. The original design opened a new
# connection for *every* run()/run_write() call, so each one paid ~1.5 s of
# pure setup before doing any work — which is why GET /api/lookups/user-types
# took 4 s to return 18 rows and a member save took ~20 s across its 13 calls.
# Connections are pooled and reused instead, so the pragmas run once per
# connection rather than once per query, and endpoints share one connection
# for the whole request.
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))

# A pooled connection can be dropped by the server or the network while it
# sits idle. Anything idle longer than this is pinged before reuse, so a dead
# connection is replaced instead of surfacing as a spurious request failure.
# (wait_timeout on this server is 8 h, so this almost never fires in practice.)
IDLE_REVALIDATE_SECONDS = 60


def _close_quietly(conn):
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()


class _Pool:
    """Fixed-capacity pool of connections, each initialised once with `setup`.

    take() blocks once POOL_SIZE connections are checked out, so a burst of
    requests can't open unbounded connections against the shared RDS box.
    """

    def __init__(self, setup, autocommit):
        self._setup = setup
        self._autocommit = autocommit
        self._idle = queue.LifoQueue()  # LIFO keeps a small working set hot
        self._slots = threading.BoundedSemaphore(POOL_SIZE)

    def _connect(self):
        conn = pymysql.connect(autocommit=self._autocommit, **DB)
        with conn.cursor() as cur:
            for stmt in self._setup:
                cur.execute(stmt)
        return conn

    def _checkout(self):
        while True:
            try:
                conn, released_at = self._idle.get_nowait()
            except queue.Empty:
                return self._connect()
            if time.monotonic() - released_at <= IDLE_REVALIDATE_SECONDS:
                return conn
            try:
                # reconnect=False on purpose: a reconnect would start a *new*
                # session without the SET SESSION pragmas above, silently
                # dropping the read-only guarantee. Discard and rebuild instead.
                conn.ping(reconnect=False)
                return conn
            except Exception:
                _close_quietly(conn)

    @contextlib.contextmanager
    def take(self):
        self._slots.acquire()
        conn = None
        try:
            conn = self._checkout()
            yield conn
        except (pymysql.Error, OSError):
            # Connection-level trouble: don't hand this one back to the pool.
            # Application errors (e.g. HTTPException) fall through and keep it.
            _close_quietly(conn)
            conn = None
            raise
        finally:
            if conn is not None:
                self._idle.put((conn, time.monotonic()))
            self._slots.release()


# autocommit=True is required, not cosmetic: with it off, every SELECT opens a
# REPEATABLE READ transaction that is never committed, and a pooled connection
# would then keep serving the same frozen snapshot for its whole lifetime —
# writes made elsewhere would appear to never land. Per-connection connections
# hid this; pooled ones don't.
READ_POOL = _Pool(("SET SESSION TRANSACTION READ ONLY", "SET SESSION group_concat_max_len = 8192"),
                  autocommit=True)
# Writes manage their own transactions via run_write_tx, hence autocommit off.
WRITE_POOL = _Pool(("SET SESSION group_concat_max_len = 8192",), autocommit=False)


@contextlib.contextmanager
def read_cursor():
    """One pooled read-only cursor for a whole request — endpoints that issue
    several queries share it instead of taking a connection per query."""
    with READ_POOL.take() as conn, conn.cursor() as cur:
        yield cur


def run(sql, args=None, one=False, cur=None):
    """Read query. Pass `cur` to reuse a cursor already open for this request."""
    if cur is not None:
        cur.execute(sql, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    with read_cursor() as c:
        return run(sql, args, one, cur=c)


def run_write_tx(fn):
    """Run fn(cursor) as one committed transaction; rolls back on error.
    Used where several statements must land atomically (create, cascading
    delete) — and now also for the existence check and the post-write re-read
    each write endpoint does, so a whole write request is one round trip's
    worth of connection setup instead of four or five."""
    with WRITE_POOL.take() as conn:
        try:
            with conn.cursor() as cur:
                result = fn(cur)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


# component_ids comes back as a comma string; the UI wants an int array.
def shape(r):
    ids = r.get("component_ids")
    r["component_ids"] = [int(x) for x in ids.split(",")] if ids else []
    return r


# S3 bucket cadre photos are stored under — tdp_cadre.image holds just the
# relative key (e.g. "152/AP1406574091.jpg"), NULL for the ~99 cadre with no photo on file.
CADRE_IMAGE_BASE = "https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/"

# Placeholder image key stamped on every cadre record created from the admin
# console's manual "Create Membership ID" flow — this console has no photo
# upload, so every such record gets the same stand-in key rather than NULL.
DEFAULT_CADRE_IMAGE = "human.jpg"

# login_otp_details has no expiry column. By product decision, the admin sets an OTP's
# expiry explicitly (Reset OTP modal's date/time picker) and it's stored in that row's
# updated_time — generated_time stays the true creation timestamp, untouched. This
# doesn't change the reference doc's documented 10-minute rule for the separate
# member-facing login-verification flow (not part of this codebase); it's specific to
# what this admin console writes and reads. OTP_DEFAULT_VALID_MINUTES is only the
# fallback used when the admin doesn't pick a time (e.g. the create-cadre flow below,
# which has no picker).
OTP_DEFAULT_VALID_MINUTES = 10

# One row per login. Collapses the member×role×component fan-out in SQL. [Backend.md §5.1]
# location_name resolves location_value (untyped int) to a human name, same CASE logic as
# CADRE_BY_MOBILE_SELECT below: 'AP' for level 2 (state-wide), else the matching
# constituency.name for level 4/5 (empty string otherwise).
#
# The "a login has at most one active access_level grant" assumption these collapsed
# level/location columns were written against is false: 99 logins currently hold more than
# one (an ASSEMBLY seat plus a PARLIAMENT seat, say). Taking MAX() of each column
# independently then mixed rows together — member 115 holds (ASSEMBLY, 173) and
# (PARLIAMENT, 510) and was reported as (ASSEMBLY, 510), a pairing that exists in no grant
# row. The ROW_NUMBER() derived table below picks one real grant per login instead, ordered
# by primary key so it is both deterministic and the same grant the old list query happened
# to surface. The MAX() wrappers stay only to satisfy GROUP BY — the join now yields at most
# one access_level row per login, so they aggregate a single value.
# Callers that need every active scope should use `locations` (MEMBER_LOCATIONS_QUERY),
# which is what the Detail screen reads; these columns remain a one-line summary.
#
# This is now also what GET /api/members serves. It used to run a separate MEMBERS_QUERY
# that returned the raw login × component fan-out (5522 rows / 1.1 MB) and collapsed it in
# Python; this aggregated form carries byte-identical data in 1430 rows / 0.26 MB, which
# over the link to RDS is the difference between ~4.5 s and ~1.2 s of transfer. The
# membership_id '#' prefix came from that query and is kept here so the UI renders the same
# MID — and so a row returned by a *write* endpoint now matches the one in the list, which
# previously disagreed (the list had the '#', MEMBER_SELECT didn't, so saving a member
# silently dropped the prefix from the displayed MID).
MEMBER_SELECT = f"""
  SELECT AM.activity_member_id, AM.member_name, AM.tdp_cadre_id, AM.inserted_time,
         AM.updated_by, AM.is_acitve, CONCAT("#", TC.membership_id) AS membership_id, TC.mobile_no,
         CONCAT("{CADRE_IMAGE_BASE}", TC.image) AS image_url,
         MAX(AMAT.user_type_id) AS role_id, MAX(UT.type) AS role_name, MAX(UT.short_name) AS role_short,
         MAX(AMAL.activity_member_level_id) AS level_id, MAX(UL.level) AS level_name,
         MAX(AMAL.activity_location_value) AS location_value,
         MAX(CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
                  WHEN AMAL.activity_member_level_id = 4 THEN PC.name
                  WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END) AS location_name,
         GROUP_CONCAT(DISTINCT AMC.component_id ORDER BY AMC.component_id) AS component_ids
  FROM activity_member AM
  LEFT JOIN tdp_cadre TC ON TC.tdp_cadre_id = AM.tdp_cadre_id
  LEFT JOIN activity_member_access_type AMAT ON AMAT.activity_member_id = AM.activity_member_id AND AMAT.is_active='Y'
  LEFT JOIN user_type UT ON UT.user_type_id = AMAT.user_type_id
  LEFT JOIN (
      SELECT activity_member_id, activity_member_level_id, activity_location_value,
             ROW_NUMBER() OVER (PARTITION BY activity_member_id
                                ORDER BY activity_member_access_level_id) AS rn
      FROM activity_member_access_level WHERE is_active='Y'
  ) AMAL ON AMAL.activity_member_id = AM.activity_member_id AND AMAL.rn = 1
  LEFT JOIN user_level UL ON UL.user_level_id = AMAL.activity_member_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  LEFT JOIN activity_member_component AMC ON AMC.activity_member_id = AM.activity_member_id AND AMC.is_valid='Y'
"""
GROUP_BY = """ GROUP BY AM.activity_member_id, AM.member_name, AM.tdp_cadre_id,
  AM.inserted_time, AM.updated_by, AM.is_acitve, TC.membership_id, TC.mobile_no, TC.image"""

# A login can actually hold more than one active access_level grant at once (e.g. an
# ASSEMBLY seat plus a PARLIAMENT seat). MEMBER_SELECT above still collapses
# that to a single MAX()'d level/location for backward compat, but the Detail screen wants
# every active location, so this fetches them separately and gets attached as `locations`.
MEMBER_LOCATIONS_QUERY = """
  SELECT AMAL.activity_member_id,
         AMAL.activity_member_level_id AS level_id, UL.level AS level_name,
         AMAL.activity_location_value AS location_value,
         CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
              WHEN AMAL.activity_member_level_id = 4 THEN PC.name
              WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END AS location_name
  FROM activity_member_access_level AMAL
  LEFT JOIN user_level UL ON UL.user_level_id = AMAL.activity_member_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  WHERE AMAL.is_active = 'Y'
"""


def attach_locations(cur, members_by_id):
    """members_by_id: {activity_member_id: member_dict}. Adds a `locations` list to each, in place.
    Runs on the caller's cursor so it shares the request's single connection."""
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

app = FastAPI(title="UA admin API")
# PUT/POST/DELETE listed here too so re-enabling the commented-out write
# endpoints below doesn't also require remembering to update this line.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "PUT", "POST", "DELETE"], allow_headers=["*"])


class RoleUpdate(BaseModel):
    user_type_id: int


class ActiveUpdate(BaseModel):
    is_active: str  # 'Y' or 'N'


class LevelUpdateItem(BaseModel):
    user_level_id: int
    location_value: Optional[int] = None


class LevelUpdate(BaseModel):
    locations: List[LevelUpdateItem]


class MemberCreate(BaseModel):
    tdp_cadre_id: int
    user_type_id: int
    locations: List[LevelUpdateItem]  # at least one — a login can hold several active scopes at once
    component_ids: List[int] = []


class ComponentGrant(BaseModel):
    component_id: int


class MemberSave(BaseModel):
    """Detail screen "Save changes". Every field is optional — omitted (None)
    means "leave this alone", so the frontend sends only what it changed."""
    user_type_id: Optional[int] = None
    is_active: Optional[str] = None
    locations: Optional[List[LevelUpdateItem]] = None
    component_ids: Optional[List[int]] = None


class CadreCreate(BaseModel):
    first_name: str
    mobile_no: str
    age: Optional[int] = None
    gender: Optional[str] = None  # 'M' or 'F', optional
    otp: Optional[str] = None  # 6 digits; server generates one if omitted
    valid_till: Optional[datetime] = None  # admin-picked expiry; defaults to +10 min if omitted


class OtpRegenerate(BaseModel):
    otp: Optional[str] = None  # 6 digits; server generates one if omitted
    valid_till: Optional[datetime] = None  # admin-picked expiry; defaults to +10 min if omitted


# 1) member list  (?status=all|active|inactive, default active)
# Served by MEMBER_SELECT above, which already collapses the login x role x
# component fan-out in SQL. The status filter is applied in SQL rather than by
# filtering the full list in Python, so a filtered call doesn't drag every
# login across the wire to throw most of them away.
# The joins are LEFT JOINs rather than inner ones — with inner joins, any login
# with zero granted components, or no active role, would vanish from the
# Active/Inactive counts entirely instead of just showing an empty
# role/component list, which would quietly undercount both KPIs. Same reasoning
# for TC: LEFT JOIN so a login whose tdp_cadre_id doesn't resolve still counts
# instead of disappearing.
STATUS_FILTER = {"active": " WHERE AM.is_acitve = 'Y'", "inactive": " WHERE AM.is_acitve = 'N'"}


@app.get("/api/members")
def members(status: str = "active"):
    with read_cursor() as cur:
        rows = run(MEMBER_SELECT + STATUS_FILTER.get(status, "") + GROUP_BY, cur=cur)
        result = [shape(r) for r in rows]
        attach_locations(cur, {m["activity_member_id"]: m for m in result})
    return result


# 2) single member (any status, so a deactivated login can still be opened)
@app.get("/api/members/{member_id}")
def member(member_id: int):
    with read_cursor() as cur:
        row = run(MEMBER_SELECT + " WHERE AM.activity_member_id = %s" + GROUP_BY, (member_id,), one=True, cur=cur)
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        row = shape(row)
        attach_locations(cur, {row["activity_member_id"]: row})
    return row


# --- shared write-endpoint helpers -----------------------------------------
# Both run on the write transaction's own cursor, so a write request needs one
# pooled connection total rather than one per query.


# Each apply_* below is one field of a login's access, written on the caller's
# transaction cursor. The single-field endpoints call one; PUT /api/members
# calls whichever the Detail screen actually changed, in one transaction.


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
    """Flip activity_member.is_acitve. Deactivating also kills any live OTPs,
    mirroring Backend.md's reference flow."""
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


def require_member(cur, member_id):
    """404 if the login doesn't exist. Every write endpoint used to run the
    full MEMBER_SELECT join just for this — a point lookup is enough."""
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


# 3) lookups
@app.get("/api/lookups/user-types")
def user_types():
    return run("SELECT user_type_id AS id, type, short_name AS short, order_no FROM user_type ORDER BY user_type_id")


@app.get("/api/lookups/user-levels")
def user_levels():
    levels = run("SELECT user_level_id AS id, level AS name FROM user_level ORDER BY user_level_id")
    return {"levels": levels, "used_level_ids": [5, 4, 2]}


@app.get("/api/lookups/components")
def components():
    return run(
        "SELECT component_id AS id, name, actual_name AS actual, dashboard_display_name AS display, order_no, is_active "
        "FROM component ORDER BY component_id"
    )


@app.get("/api/lookups/constituencies")
def constituencies():
    return run(
        "SELECT * FROM constituency "
        "WHERE state_id = 1 AND deform_date IS NULL AND election_scope_id = 2 "
        "GROUP BY constituency_id"
    )


@app.get("/api/lookups/parliaments")
def parliaments():
    return run(
        "SELECT C.constituency_id, C.name, C.election_scope_id "
        "FROM constituency C "
        "JOIN election E ON E.election_scope_id = C.election_scope_id "
        "WHERE C.election_scope_id = 1 AND C.state_id = 1 "
        "AND E.election_year = 2024 AND C.deform_date IS NULL"
    )


# 4) cadre MID lookup (create-flow step 1, read-only)
@app.get("/api/cadre/{mid}")
def cadre(mid: str):
    row = run(
        f"SELECT tdp_cadre_id, membership_id, first_name, last_name, mobile_no, "
        f"gender, constituency_id, CONCAT('{CADRE_IMAGE_BASE}', image) AS image_url "
        f"FROM tdp_cadre WHERE membership_id = %s AND is_deleted = 'N' LIMIT 1",
        (mid,), one=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="no cadre for that MID")
    return row


# 4a) create a brand-new tdp_cadre record + its enrollment-year rows + its
# first login_otp_details row (Create Membership ID → manual path, no
# existing cadre matched). Mirrors steps 1-2 of the reference doc's 6-step
# workflow (Track 1: identity + auth), plus tdp_cadre_enrollment_year, which
# the reference doc doesn't cover.
# The MID is NOT admin-chosen: tdp_cadre_id is the auto-increment PK, so the
# row is inserted first (membership_id left NULL), then membership_id is set
# to str(tdp_cadre_id) in the same transaction — that guarantees uniqueness
# for free (no collision-retry needed, unlike a randomly-picked MID) and
# keeps the login key and the internal id in lockstep by design. Every such
# record also gets a placeholder image key (no photo upload in this console)
# and is_synced='Y' (this row didn't come from a field-sync device), plus a
# static enrollment_year=2014 (product decision — not derived from the current
# year or from the enrollment_year_id rows below). It's
# also enrolled in both the 2022-2024 and 2024-2026 cycles (enrollment_year_id
# 6 and 7) via two tdp_cadre_enrollment_year rows, per product decision — not
# just whichever cycle happens to be current.
# There is no SMS gateway wired into this backend — the generated OTP is
# returned directly in the response for the admin UI to display, not texted.
# `otp`/`valid_till` let the admin pick both instead of accepting the
# server-generated 6-digit code and default +OTP_DEFAULT_VALID_MINUTES expiry
# — same optional-override pattern as OtpRegenerate/POST .../otp/regenerate.
@app.post("/api/cadre", status_code=201)
def create_cadre(body: CadreCreate):
    first_name = body.first_name.strip()
    mobile = body.mobile_no.strip()
    if not first_name:
        raise HTTPException(status_code=400, detail="first_name is required")
    if not re.fullmatch(r"\d{10}", mobile):
        raise HTTPException(status_code=400, detail="mobile_no must be exactly 10 digits")

    gender = (body.gender or "").strip().upper() or None
    if gender and gender not in ("M", "F"):
        raise HTTPException(status_code=400, detail="gender must be 'M' or 'F'")
    if body.age is not None and not (0 < body.age < 150):
        raise HTTPException(status_code=400, detail="age must be a realistic value")

    requested_otp = (body.otp or "").strip()
    if requested_otp and not re.fullmatch(r"\d{6}", requested_otp):
        raise HTTPException(status_code=400, detail="otp must be exactly 6 digits")
    if body.valid_till and body.valid_till <= datetime.now():
        raise HTTPException(status_code=400, detail="valid_till must be in the future")

    def _create(cur):
        cur.execute(
            "INSERT INTO tdp_cadre (first_name, mobile_no, gender, age, image, "
            "enrollment_year, is_deleted, is_synced, data_source_type, inserted_time) "
            "VALUES (%s, %s, %s, %s, %s, 2014, 'N', 'Y', 'WEB', NOW())",
            (first_name, mobile, gender, body.age, DEFAULT_CADRE_IMAGE),
        )
        cadre_id = cur.lastrowid
        mid = str(cadre_id)
        cur.execute("UPDATE tdp_cadre SET membership_id=%s WHERE tdp_cadre_id=%s", (mid, cadre_id))

        # Enrollment-year membership: this console always enrolls a newly-created
        # cadre in both the 2022-2024 and 2024-2026 cycles (enrollment_year_id 6
        # and 7 — see `enrollment_year` lookup), one row each, rather than just
        # the currently-active cycle.
        for enrollment_year_id in (6, 7):
            cur.execute(
                "INSERT INTO tdp_cadre_enrollment_year "
                "(tdp_cadre_id, enrollment_year_id, inserted_date, inserted_time, is_deleted) "
                "VALUES (%s, %s, CURDATE(), NOW(), 'N')",
                (cadre_id, enrollment_year_id),
            )

        # generated_time is a fixed '2026-12-31' for a brand-new MID, not NOW()
        # — product decision. The real admin-picked expiry still goes in
        # updated_time (see the OTP-expiry note in CLAUDE.md). Note this is the
        # column the separate member-facing login flow window-checks.
        otp = requested_otp or f"{random.randint(0, 999999):06d}"
        expires_at = body.valid_till or (datetime.now() + timedelta(minutes=OTP_DEFAULT_VALID_MINUTES))
        cur.execute(
            "UPDATE login_otp_details SET is_valid='N', updated_time=NOW() "
            "WHERE tdp_cadre_id=%s AND is_valid='Y'",
            (cadre_id,),
        )
        cur.execute(
            "INSERT INTO login_otp_details "
            "(tdp_cadre_id, membership_id, mobile_no, otp, generated_time, updated_time, is_valid) "
            "VALUES (%s, %s, %s, %s, '2026-12-31', %s, 'Y')",
            (cadre_id, mid, mobile, otp, expires_at),
        )
        return {
            "tdp_cadre_id": cadre_id, "membership_id": mid,
            "first_name": first_name, "mobile_no": mobile,
            "gender": gender, "age": body.age,
            "image_url": f"{CADRE_IMAGE_BASE}{DEFAULT_CADRE_IMAGE}",
            "otp": otp, "expires_at": expires_at,
        }

    return run_write_tx(_create)


# 4b) cadre lookup by mobile number (create-flow step 1, read-only).
# mobile_no is indexed but NOT unique — multiple cadre (e.g. family members
# sharing one phone) can share a number, so this returns every match
# (possibly []). Column aliases are kept as given by the admin's reference
# queries (not this file's usual snake_case) so the response stays
# recognisable against the source SQL. Base FROM/LEFT JOIN shape (starting at
# tdp_cadre, not activity_member) and the mobile_no filter are preserved from
# the original version of this query on purpose — see the two gotchas below —
# with LOCATION folded in from a second reference query that otherwise
# INNER JOINed from activity_member and had no mobile_no filter at all, which
# would have dropped the "cadre has no login yet" case this endpoint exists
# for. Things worth knowing:
#   - AM only joins on is_acitve='Y', so a *deactivated* login reads
#     identical to "no login yet" here (AMID/LOCLEVEL/TEAMNAME all null) —
#     unlike create_member's duplicate check below, which still counts a
#     deactivated activity_member row as "already exists" and refuses to
#     create a second one.
#   - No GROUP BY: a cadre with more than one active role or access-level
#     grant at once (rare in this data) fans out into multiple rows.
#   - LOCATION resolves LOCVALUE (an untyped int) to a human name: 'AP' for
#     level 2 (state-wide), else a LEFT JOIN to `constituency` keyed off
#     activity_location_value for level 4/5, empty string for anything else.
#     PC/AC are two separate joins to the same table (one per level) so the
#     CASE can pick the right one without ambiguity.
# is_deleted='N' was added (not in the source query) so a deleted cadre
# record can't show up here and then 404 when picked for creation.
CADRE_BY_MOBILE_SELECT = f"""
  SELECT
      CR.tdp_cadre_id AS CADREID,
      UPPER(CR.first_name) AS MEMBERNAME,
      CR.mobile_no AS MOBILENO,
      CONCAT('#', CR.membership_id) AS MID,
      CONCAT("{CADRE_IMAGE_BASE}", CR.image) AS IMAGE,
      AM.activity_member_id AS AMID,
      UL.level AS LOCLEVEL,
      AMAL.activity_location_value AS LOCVALUE,
      CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
           WHEN AMAL.activity_member_level_id = 4 THEN PC.name
           WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END AS LOCATION,
      CONCAT('#', LOD.otp) AS OTP,
      CONCAT('#', DATE(LOD.generated_time)) AS EXPDATE,
      UT.short_name AS TEAMNAME
  FROM tdp_cadre CR
  LEFT JOIN activity_member AM ON CR.tdp_cadre_id = AM.tdp_cadre_id AND AM.is_acitve = 'Y' AND AM.activity_member_id <> 581
  LEFT JOIN activity_member_access_level AMAL ON AM.activity_member_id = AMAL.activity_member_id AND AMAL.is_active = 'Y'
  LEFT JOIN user_level UL ON AMAL.activity_member_level_id = UL.user_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  LEFT JOIN activity_member_access_type AMAT ON AM.activity_member_id = AMAT.activity_member_id AND AMAT.is_active = 'Y'
  LEFT JOIN user_type UT ON AMAT.user_type_id = UT.user_type_id
  LEFT JOIN login_otp_details LOD ON CR.tdp_cadre_id = LOD.tdp_cadre_id AND LOD.is_valid = 'Y'
  WHERE CR.mobile_no = %s AND CR.is_deleted = 'N'
  ORDER BY CR.tdp_cadre_id, UL.user_level_id, UT.order_no
"""


@app.get("/api/cadre/by-mobile/{mobile}")
def cadre_by_mobile(mobile: str):
    return run(CADRE_BY_MOBILE_SELECT, (mobile,))


# 4c) access-type grant count for a MID (read-only, standalone check — NOT used
# by MEMBER_SELECT above). That query collapses
# activity_member_access_type with MAX() on the assumption a login
# has at most one active role grant at a time (see comments at MEMBER_SELECT
# and CADRE_BY_MOBILE_SELECT). This exists purely to check that assumption for
# a given MID by listing every access_type row (active or not) tied to it,
# instead of trusting the aggregated columns.
ACCESS_TYPES_BY_MID_SELECT = """
  SELECT AMAT.activity_member_access_type_id, AMAT.activity_member_id,
         AMAT.user_type_id, UT.type AS role_name, UT.short_name AS role_short,
         AMAT.is_active
  FROM tdp_cadre TC
  JOIN activity_member AM ON AM.tdp_cadre_id = TC.tdp_cadre_id
  JOIN activity_member_access_type AMAT ON AMAT.activity_member_id = AM.activity_member_id
  LEFT JOIN user_type UT ON UT.user_type_id = AMAT.user_type_id
  WHERE TC.membership_id = %s
  ORDER BY AM.activity_member_id, AMAT.is_active DESC, AMAT.user_type_id
"""


@app.get("/api/cadre/{mid}/access-types")
def cadre_access_types(mid: str):
    rows = run(ACCESS_TYPES_BY_MID_SELECT, (mid,))
    active = [r for r in rows if r["is_active"] == "Y"]
    return {
        "membership_id": mid,
        "total_grants": len(rows),
        "active_grants": len(active),
        "grants": rows,
    }


# --- WRITE ENDPOINTS -------------------------------------------------------
# Create/role/active/level/delete for a login. Re-enabled -- see git history
# (commit 214eae3) for why these were briefly disabled pending auth; there is
# still no authentication in front of this API (see module docstring above).
# 5) create a login (New login → cadre found, no activity_member yet).
# A cadre only gets dashboard access once it has an activity_member row plus
# its three grant rows (role/level/components) — mirrors the reference doc's
# 6-step workflow, steps 3-6. Refuses to create a second login for a cadre
# that already has one (activity_member_id 581 is a reserved/placeholder
# record and is ignored for this check, per the source query this was built
# from) — use the role/level/active endpoints to change an existing login
# instead of creating a duplicate.
@app.post("/api/members", status_code=201)
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


# 6) change a login's role (New login → existing-login panel).
# Deactivates any currently-active role grant(s), then reactivates a matching
# prior grant or inserts a fresh one — mirrors the reference doc's step 6.
@app.put("/api/members/{member_id}/role")
def update_role(member_id: int, body: RoleUpdate):
    def _update(cur):
        require_member(cur, member_id)
        apply_role(cur, member_id, body.user_type_id)
        return member_row(cur, member_id)

    return run_write_tx(_update)


# 7) activate / deactivate a login (New login → existing-login panel).
# Deactivating also kills any live OTPs, mirroring Backend.md's reference flow.
@app.put("/api/members/{member_id}/active")
def update_active(member_id: int, body: ActiveUpdate):
    if body.is_active not in ("Y", "N"):
        raise HTTPException(status_code=400, detail="is_active must be 'Y' or 'N'")

    def _update(cur):
        apply_active(cur, member_id, body.is_active)
        return member_row(cur, member_id)

    return run_write_tx(_update)


# 8) change a login's geographic scope (Detail screen). A login can hold
# several active access_level grants at once (see MEMBER_LOCATIONS_QUERY
# above), so this replaces the whole active set in one transaction: every
# currently-active grant is deactivated, then each requested location is
# reactivated (if a matching row already exists) or inserted — same
# reactivate-or-insert pattern as the role endpoint, just per-location and
# atomic across all of them. location_value is compared with <=> (NULL-safe
# equals) since a level like STATE may legitimately carry no location_value.
@app.put("/api/members/{member_id}/level")
def update_level(member_id: int, body: LevelUpdate):
    def _update(cur):
        require_member(cur, member_id)
        apply_locations(cur, member_id, body.locations)
        return member_row(cur, member_id, with_locations=True)

    return run_write_tx(_update)


# 8b) grant a personal component to a login (Detail screen "Add component").
# Reactivates a prior (revoked) grant for the same component if one exists,
# rather than inserting a duplicate row, same reactivate-or-insert pattern as
# the role/level endpoints above.
def grant_components(cur, member_id, component_ids):
    """Reactivate-or-insert each component grant. Shared by the single-component
    endpoint and the batch one below."""
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
    if not component_ids:
        return
    placeholders = ",".join(["%s"] * len(component_ids))
    cur.execute(
        f"UPDATE activity_member_component SET is_valid='N' "
        f"WHERE activity_member_id=%s AND component_id IN ({placeholders}) AND is_valid='Y'",
        [member_id, *component_ids],
    )


@app.post("/api/members/{member_id}/components")
def add_component(member_id: int, body: ComponentGrant):
    def _add(cur):
        require_member(cur, member_id)
        grant_components(cur, member_id, [body.component_id])
        return member_row(cur, member_id)

    return run_write_tx(_add)


# 8d) apply a whole Detail-screen "Save changes" in one request and one
# transaction. The frontend used to fire this as up to four sequential
# requests (role, active, level, then one per component), each of which paid
# its own existence check, member re-SELECT and COMMIT — measured at ~59 s for
# a member with five components. Only the fields present in the body are
# touched, so the frontend still sends only what actually changed; the
# difference is that they now land together instead of one round trip at a
# time. Atomic as a side effect: a failure part-way no longer leaves the login
# with its new role but its old scope.
@app.put("/api/members/{member_id}")
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


# 8c) revoke a personal component from a login (Detail screen "Remove
# component"). Soft-revoke only — flips is_valid='N', mirroring every other
# grant table's delete semantics — so re-adding later reactivates the same
# row instead of accumulating duplicates.
@app.delete("/api/members/{member_id}/components/{component_id}")
def remove_component(member_id: int, component_id: int):
    def _remove(cur):
        require_member(cur, member_id)
        revoke_components(cur, member_id, [component_id])
        return member_row(cur, member_id)

    return run_write_tx(_remove)


# 9) soft-delete a login (Detail screen). Distinct from deactivate: cascades
# is_active/is_valid='N' across every grant table (role, level, components),
# not just activity_member.is_acitve, so a later reactivate comes back with
# no stale grants rather than silently restoring the old access set.
@app.delete("/api/members/{member_id}")
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


# 10) OTP for an existing login (Users list "Reset OTP" action). Mirrors the
# reference doc's Step 2 (Dakavara_PA_Dashboard_User_Creation_Reference.md
# §"Generate OTP"): a login's OTP lives in login_otp_details keyed off
# tdp_cadre_id, not activity_member_id, so both endpoints below resolve that
# first. expires_at is read straight off updated_time (see the
# OTP_DEFAULT_VALID_MINUTES comment above for why that column holds it).


NO_OTP = {"otp": None, "generated_time": None, "expires_at": None, "is_valid": "N", "is_expired": True}


def _otp_status(cur, tdp_cadre_id):
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


@app.get("/api/members/{member_id}/otp")
def member_otp(member_id: int):
    with read_cursor() as cur:
        login = run("SELECT tdp_cadre_id FROM activity_member WHERE activity_member_id=%s",
                    (member_id,), one=True, cur=cur)
        if not login:
            raise HTTPException(status_code=404, detail="not found")
        if not login["tdp_cadre_id"]:
            return dict(NO_OTP)
        return _otp_status(cur, login["tdp_cadre_id"])


# Invalidates any live OTP and inserts a fresh one, same
# invalidate-then-insert order as the reference doc and POST /api/cadre
# above. `otp` in the body lets the frontend save a value it already staged
# for preview in the "Reset OTP" modal; omitted, the server generates one.
# `valid_till` is the admin-picked expiry from that same modal, stored in the
# new row's updated_time; omitted, it defaults to +OTP_DEFAULT_VALID_MINUTES.
@app.post("/api/members/{member_id}/otp/regenerate")
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
        return _otp_status(cur, login["tdp_cadre_id"])

    return run_write_tx(_regen)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4000)