"""Program meetings, and the Programmes page's role/activity/leader tracking.

The `/{program_id}/...` routes below are a different, still-unwired feature
(individual programme *events*, mirroring the `meetings` domain) — their
contract survives the removal of upstream wiring, the bodies do not.

The role/activity-summary/leaders routes are real and live off four
`party_track` tables: `role` (already populated, shared with `/roles`),
`leader` (the 71k-row real roster), `program` and `program_role` (the
catalog of trackable programmes and which roles they apply to), and
`leader_program_activity` (one row per leader/programme/month once someone
records an update). The last three are freshly added and were still empty in
production when this was written, so Updated/Not updated and the whole
activity-summary card read zero/empty until rows start arriving —
Total/Members do not, since those come straight off the `leader` roster. The
one thing in this service that writes `leader_program_activity` is
`save_leader_monthly_activity`, for the three programmes listed below.

`party_track.activity` is a separate, long-lived scoring system
(`leader_activity`, ~800k rows, weighted points) unrelated to this feature.
`/activities` below now serves `party_track.program` instead — the
Programmes page's own "Activity" column and filter are `program`, since
`leader_program_activity.program_id` is what it actually joins against.

`/leaders` reads neither of those for its `attended` fact: two groups of
programmes report a real one, both off `mytdp`, not `party_track` —
Calendar Meetings from `meeting_invitee`/`meeting_attendance`
(`_is_calendar_meetings`), and the three monthly activities from
`mytdp.program`/`program_attendance` (`_is_monthly_activity`,
`_monthly_activity_mytdp_filter`) — see the branches in `program_leaders`.

Every roster count and member list here is narrowed to the assemblies the caller
was granted, through `leader.constituency_id` — the same id `mytdp.assembly.id`
uses (see `access.py`). A role with no leader inside the caller's assemblies
drops out of `/roles` and both summary cards entirely, rather than showing as a
row of zeroes.

Each programme's Update modal is backed by one of three tables, and which
one is decided by the programme's name alone:

* **Calendar Meeting** — `leader_meeting_attendance`, keyed by the real
  `mytdp` meeting the leader was invited to: remarks via
  `save_leader_meeting_remarks`, a file via `upload_leader_meeting_file`
  (S3, `app/storage.py` — the same account the monthly activities below
  use), both read back through `program_leader_meetings`.
* **Pedala Sevalo, Swatch Andhra, Pattadar Passbook** — remarks go to
  `leader_program_activity`, one row per leader/programme/month
  (`_MONTHLY_ACTIVITY_PROGRAMS`, `program_leader_monthly_activity`,
  `save_leader_monthly_activity`); this is the same table the two summary
  cards count Updated/Not updated from, so recording a remark here is what
  moves those figures for these three. **Attendance itself is not stored
  there** — `party_track` has no invitee/attendance concept of its own for
  these, so `/leaders`' `attended` for this trio reads real check-in data
  out of `mytdp.program`/`program_attendance` instead (see above).
* **everything else** — `leader_meetings`, a dated list the leader adds to
  by hand (`program_leader_log_entries`, `add_leader_log_entry`); a file
  goes on afterwards, by the new row's own id
  (`upload_leader_log_entry_file`), the same S3 account as the two groups
  above.

The three are fenced off `leader_meetings` rather than merely pointed
elsewhere: the log-entry routes raise for them instead of writing a row no
reader would look at again.
"""

from __future__ import annotations

import calendar
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from .. import access, config, db, storage
from ..access import Scope
from ..auth import caller_scope

router = APIRouter(prefix="/api/programs", tags=["programs"])

_UNWIRED = "No data source configured"

# Matches the frontend's own `isCalendarMeetingsActivity` — the one programme
# name that gets the mytdp-backed branch below rather than leader_program_activity.
_CALENDAR_MEETINGS_RE = re.compile(r"calendar\s*meetings?", re.IGNORECASE)

# party_track.attendance_type: 1 Attended, 2 Conducted, 3 Absent, 4 Not
# Applicable. Only the first two of those describe one leader's own presence
# at one meeting — "Conducted" is about the meeting itself, not read here.
_ATTENDANCE_TYPE_ATTENDED = 1
_ATTENDANCE_TYPE_ABSENT = 3

# The programmes recorded as one `leader_program_activity` row per
# leader/month rather than as a dated list in `leader_meetings`. That table
# has a `month_id` and no date column at all, so a month is the finest period
# it can key a row by — hence one monthly record per leader, not an entry per
# outing.
#
# Matched on the name, like `_is_calendar_meetings`, rather than on the live
# ids (1, 6, 7): `program` is a hand-seeded 10-row table, and a name is the
# thing the frontend and this module can agree on without either holding a
# copy of the other's ids. Names are compared through `_norm_program`, so
# stray or doubled whitespace in the seed data does not decide which table a
# programme's data lands in.
_MONTHLY_ACTIVITY_PROGRAMS = frozenset({
    "pedala sevalo",
    "swatch andhra",
    "pattadar passbook",
})


def _norm_program(program_name: str | None) -> str:
    return re.sub(r"\s+", " ", str(program_name or "").strip().lower())


def _is_calendar_meetings(program_name: str | None) -> bool:
    return bool(program_name and _CALENDAR_MEETINGS_RE.search(program_name))


def _is_monthly_activity(program_name: str | None) -> bool:
    return _norm_program(program_name) in _MONTHLY_ACTIVITY_PROGRAMS


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """First and last calendar date of the month, as `YYYY-MM-DD` strings —
    used to test a `mytdp.program` row's `[from_date, to_date]` span for
    overlap with the requested month."""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


# `mytdp.program_type.code = 'Pedala Sevalo'` — the one of the three monthly
# activities with a dedicated, reliable type (verified: all 13 `program` rows
# under this id are the same recurring "పేదల సేవలో" event, one per month).
_PEDALA_SEVALO_PROGRAM_TYPE_ID = "e0837147a-3eda-11f0-a697-c853090bd99"


def _monthly_activity_mytdp_filter(program_name: str) -> tuple[str, tuple]:
    """A boolean SQL fragment over an aliased `mp` row from `mytdp.program`
    (plus its bound params) selecting the real event rows behind one of the
    three monthly activities, for the real-attendance join in
    `program_leaders`.

    `mytdp.program_type` has no row at all for two of the three — Swatch
    Andhra and Pattadar Passbook are filed under three different types
    between them (Dharna, Event, Other) with nothing to key on but the
    programme's own Telugu name — so those two are matched by name instead.
    Verified against a live dump of all 119 `mytdp.program` rows on
    2026-08-22: 'స్వచ్ఛ' and 'ఆంధ్ర' both appear in exactly the three
    "స్వచ్ఛ(ాంధ్ర / ఆంధ్ర) ... స్వర్ణాంధ్ర" rows and nowhere else; 'పట్టాదార్'
    appears in exactly the three "పట్టాదార్ పాస్ బుక్ పంపిణీ" rows and
    nowhere else. If the party starts naming an unrelated programme with
    either word, this starts over-matching — there is no structural
    guarantee against that, only the observed data.
    """
    name = _norm_program(program_name)
    if name == "pedala sevalo":
        return "mp.program_type_id = %s", (_PEDALA_SEVALO_PROGRAM_TYPE_ID,)
    if name == "swatch andhra":
        return "mp.name LIKE %s AND mp.name LIKE %s", ("%స్వచ్ఛ%", "%ఆంధ్ర%")
    if name == "pattadar passbook":
        return "mp.name LIKE %s", ("%పట్టాదార్%",)
    raise ValueError(f"{program_name!r} is not a monthly activity")


def _leader_with_role_sql() -> str:
    """A leader's role(s) come from `leader_role` (the many-to-many mapping
    table), not just the `role_id` column on `leader` itself — a Minister
    is routinely also an MLA (all 21 active Ministers hold both today,
    `leader_role` rows `1,2`), and that second row must not be dropped, so
    this fans a leader with N rows in `leader_role` out to N rows here, one
    per role — a Minister/MLA counts under both the Minister and MLA cards,
    the same person, not a duplicate to be collapsed. `leader_role` also
    stopped being backfilled for leaders added after leader_id 64366, so
    ~7.2k of the 68k active party leaders have no row there at all; for
    those this falls back to `leader.role_id` (a plain LEFT JOIN keeps
    exactly one row when there is no match), so a leader missing from
    `leader_role` still appears under their own role rather than vanishing.
    Every caller that used to join straight to `{PARTY_TRACK_DB}.leader` for
    role filtering joins to this instead — same column names (plus the
    fan-out), so the rest of each query is unchanged; callers that count
    distinct leaders rather than role-slots should use
    `COUNT(DISTINCT l.leader_id)`, not `COUNT(l.leader_id)`.

    The trailing `LIMIT 18446744073709551615` (max BIGINT UNSIGNED, i.e. no
    real limit) is not decorative: without it MySQL merges this derived
    table into the outer query instead of materialising it, so a join on
    the computed `role_id` column — `program_role`/`role` do this, `leader`
    itself never needed to since its own `role_id` is a real column — can't
    use an index and falls back to a per-outer-row scan of all ~71k leader
    rows. `/activity-summary` measured 17s that way and 0.8s once the LIMIT
    forced materialisation (which also lets MySQL auto-index the computed
    column). A LIMIT is one of the few things that reliably blocks merge
    optimisation; do not remove it as unnecessary or the endpoint is slow
    again with no visible error."""
    return f"""(
        SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
               l.constituency_id, l.parliament_id, l.is_deleted, l.party_id,
               COALESCE(lr.role_id, l.role_id) AS role_id
          FROM {config.PARTY_TRACK_DB}.leader l
          LEFT JOIN {config.PARTY_TRACK_DB}.leader_role lr ON lr.leader_id = l.leader_id
         LIMIT 18446744073709551615
    )"""


def _active_role_ids(scope: Scope) -> list[int]:
    """Role ids with at least one active party leader, by the same effective
    role `_leader_with_role_sql` computes. `/role-summary` and
    `/activity-summary` each used to run that derived table a second time
    inside a correlated `EXISTS` per candidate role — cheap-looking SQL, but
    the derived table itself aggregates all of `leader_role` (64k rows), and
    running that once per `program_role` row (~40) rather than once total
    was what made `/activity-summary` take 18s. Computed once here and
    reused as a plain `role_id IN (...)` list instead."""
    rows = db.rows(
        f"""SELECT DISTINCT role_id FROM {_leader_with_role_sql()} l
             WHERE is_deleted = 'N' AND party_id = %s AND {access.leader(scope)}""",
        (config.LEADER_PARTY_ID,),
    )
    return [int(r["role_id"]) for r in rows if r["role_id"] is not None]


def _month_id(year: int, month: int) -> int | None:
    """`party_track.month` is a short, hand-seeded lookup table — a period
    outside it (the current calendar month usually is, until someone extends
    it) has no leader_program_activity rows to find either way, so `None`
    here just means the WHERE below matches nothing, not an error."""
    return db.scalar(
        f"SELECT month_id FROM {config.PARTY_TRACK_DB}.month WHERE year = %s AND month_no = %s",
        (year, month),
    )


@router.get("")
async def list_programs(
    from_date: str = Query(..., alias="from", description="YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="YYYY-MM-DD"),
) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail=_UNWIRED)


@router.get("/roles")
def list_roles(scope: Scope = Depends(caller_scope)) -> list[dict[str, Any]]:
    """Every member designation a programme can be filtered by (Minister, MLA,
    MP, Mandal President, …).

    This is `party_track.role`, not `mytdp.role`: the latter is committee-
    meeting org tiers (State/District/.../Booth), a different taxonomy that
    answers a different question.

    Not every row in `role` belongs here, so the roster is narrowed two ways:
    joined against `leader` (a role with nobody actually holding it has no
    Programmes data to show), scoped to `config.LEADER_PARTY_ID` there since
    `leader` holds a handful of rows for other parties, and checked against
    `program_role` (a role no programme has been configured for is not one
    this feature can filter by). All three conditions come from real,
    already-populated tables, the same ones `/role-summary` and
    `/activity-summary` read.
    """
    active_role_ids = _active_role_ids(scope)
    if not active_role_ids:
        return []
    id_list = ",".join(str(rid) for rid in active_role_ids)
    rows = db.rows(
        f"""SELECT r.role_id, r.role_name
              FROM {config.PARTY_TRACK_DB}.role r
             WHERE (r.is_active IS NULL OR r.is_active = 'Y')
               AND r.role_id IN ({id_list})
               AND EXISTS (
                     SELECT 1 FROM {config.PARTY_TRACK_DB}.program_role pr
                      WHERE pr.role_id = r.role_id AND (pr.is_deleted IS NULL OR pr.is_deleted = 'N')
                   )
             ORDER BY (r.order_no IS NULL), r.order_no, r.role_name"""
    )
    return [{"id": r["role_id"], "name": r["role_name"]} for r in rows]


@router.get("/activities")
def list_activities() -> list[dict[str, Any]]:
    """Every programme a leader can log participation against (Calendar
    Meeting, Pedala Sevalo, Cadre Meetings, …) — `party_track.program`, the
    same table `/activity-summary` joins through `program_role`. This is what
    the Programmes page calls "Activity"; `party_track.activity` is a
    different, unrelated scoring system and is not read here."""
    rows = db.rows(
        f"SELECT program_id, program_name FROM {config.PARTY_TRACK_DB}.program ORDER BY program_id"
    )
    return [{"id": r["program_id"], "name": r["program_name"]} for r in rows]


@router.get("/role-summary")
def role_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """The first Programmes card: every role `/roles` lists — an active
    leader and a `program_role` mapping, same as there — its `leader` roster
    size, and how many of those leaders logged at least one
    `leader_program_activity` row (against any programme) in this month.

    `total` counts every leader row for the role, `members` only the ones
    not soft-deleted — the two differ exactly where leaders have left since
    being added, the same way `ACI`'s 50 rows split into 42 active today.
    Both are scoped to `config.LEADER_PARTY_ID`, same as `/leaders`.
    """
    month_id = _month_id(year, month)
    active_role_ids = _active_role_ids(scope)
    if not active_role_ids:
        return []
    id_list = ",".join(str(rid) for rid in active_role_ids)
    rows = db.rows(
        f"""SELECT r.role_id, r.role_name,
                   COUNT(l.leader_id) AS total,
                   SUM(l.is_deleted = 'N') AS members,
                   SUM(l.is_deleted = 'N' AND u.leader_id IS NOT NULL) AS updated
              FROM {config.PARTY_TRACK_DB}.role r
              LEFT JOIN {_leader_with_role_sql()} l ON l.role_id = r.role_id AND l.party_id = %s
                     AND {access.leader(scope)}
              LEFT JOIN (
                    SELECT DISTINCT leader_id FROM {config.PARTY_TRACK_DB}.leader_program_activity
                     WHERE month_id = %s AND (is_deleted IS NULL OR is_deleted = 'N')
                   ) u ON u.leader_id = l.leader_id
             WHERE (r.is_active IS NULL OR r.is_active = 'Y')
               AND r.role_id IN ({id_list})
               AND EXISTS (
                     SELECT 1 FROM {config.PARTY_TRACK_DB}.program_role pr
                      WHERE pr.role_id = r.role_id AND (pr.is_deleted IS NULL OR pr.is_deleted = 'N')
                   )
             GROUP BY r.role_id, r.role_name
             ORDER BY (r.order_no IS NULL), r.order_no, r.role_name""",
        (config.LEADER_PARTY_ID, month_id),
    )
    out = []
    for r in rows:
        members = int(r["members"] or 0)
        updated = int(r["updated"] or 0)
        out.append({
            "role": r["role_name"],
            "total": int(r["total"] or 0),
            "members": members,
            "updated": updated,
            "notUpdated": members - updated,
        })
    return out


@router.get("/activity-summary")
def activity_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """The second Programmes card: one row per (role, programme) pairing
    `program_role` actually defines — not every role crossed with every
    programme, since not every programme applies to every role — narrowed to
    roles with an active leader, same as `/roles` and `/role-summary`
    (a role nobody holds has no members to break down by programme), both
    scoped to `config.LEADER_PARTY_ID`. `activity` in the response is
    `program.program_name`; `roleId`/`activityId` ride along so the member
    card below can ask for one pairing's leaders without re-deriving ids
    from the display names."""
    month_id = _month_id(year, month)
    active_role_ids = _active_role_ids(scope)
    if not active_role_ids:
        return []
    id_list = ",".join(str(rid) for rid in active_role_ids)
    rows = db.rows(
        f"""SELECT r.role_id, r.role_name, p.program_id, p.program_name,
                   SUM(l.is_deleted = 'N') AS total_members,
                   SUM(l.is_deleted = 'N' AND u.leader_id IS NOT NULL) AS updated
              FROM {config.PARTY_TRACK_DB}.program_role pr
              JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = pr.role_id
              JOIN {config.PARTY_TRACK_DB}.program p ON p.program_id = pr.program_id
              LEFT JOIN {_leader_with_role_sql()} l ON l.role_id = pr.role_id AND l.party_id = %s
                     AND {access.leader(scope)}
              LEFT JOIN (
                    SELECT DISTINCT leader_id, program_id FROM {config.PARTY_TRACK_DB}.leader_program_activity
                     WHERE month_id = %s AND (is_deleted IS NULL OR is_deleted = 'N')
                   ) u ON u.leader_id = l.leader_id AND u.program_id = pr.program_id
             WHERE (pr.is_deleted IS NULL OR pr.is_deleted = 'N')
               AND pr.role_id IN ({id_list})
             GROUP BY r.role_id, r.role_name, p.program_id, p.program_name
             ORDER BY (r.order_no IS NULL), r.order_no, r.role_name, p.program_name""",
        (config.LEADER_PARTY_ID, month_id),
    )
    out = []
    for r in rows:
        total_members = int(r["total_members"] or 0)
        updated = int(r["updated"] or 0)
        out.append({
            "role": r["role_name"],
            "roleId": r["role_id"],
            "activity": r["program_name"],
            "activityId": r["program_id"],
            "totalMembers": total_members,
            "updated": updated,
            "notUpdated": total_members - updated,
        })
    return out


@router.get("/leaders")
def program_leaders(
    role_id: int = Query(...),
    activity_id: int = Query(..., description="program_id, despite the name — matches activity-summary's field"),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """The third Programmes card: every active leader in one role, for one
    programme and month — one row per roster member, whether or not anything
    has been recorded against the programme for them yet.

    **The card reports no counts.** It used to carry `participated`/
    `completed` straight off `leader_program_activity.total`/`.completed`,
    two columns nothing in this service has ever written, so every leader on
    every screen read 0/0; they are gone rather than left reading zero.
    Whether a leader has been updated this month is the Updated/Not updated
    pair on the two summary cards above, which count real rows.

    `assembly`/`parliament` are `mytdp` tables, not `party_track` —
    `leader.constituency_id`/`parliament_id` reach across schemas the same
    way `config.PARTY_TRACK_DB`'s own docstring says the RDS user allows.
    `mobile`/`cadreId` are `leader.mobile_no`/`tdp_cadre_id` — plain columns
    on the same row, no further join needed. Scoped to `config.LEADER_PARTY_ID`
    — `leader` holds a handful of rows for other parties. Capped at
    `MAX_PAGE_SIZE` for the same reason the meeting member table is.

    Two groups of programmes report a real `attended` fact, both off
    `mytdp`, neither off anything this service itself writes:

    * **Calendar Meetings** (see `_is_calendar_meetings`) — whether this
      leader has a `meeting_attendance` row for any meeting they were
      invited to that month, whatever its `status`, the same "attended"
      definition `/api/meetings` itself uses. `meeting_invitee.tdp_cadre_id`
      is the join key into `party_track.leader`.
    * **`_MONTHLY_ACTIVITY_PROGRAMS`** (Pedala Sevalo, Swatch Andhra,
      Pattadar Passbook) — whether this leader has a `program_attendance`
      row (`pa.user_id`, the same `tdp_cadre_id`) for any `mytdp.program`
      event belonging to that activity whose `[from_date, to_date]` span
      overlaps the requested month — see `_monthly_activity_mytdp_filter`.
      This is a real check-in fact, the same kind Calendar Meetings reports,
      not anything derived from `leader_program_activity` — that table
      never carries a leader's presence, only the remark
      `save_leader_monthly_activity` writes alongside it.

    **Every other programme returns no field at all**, rather than a false
    one, since no invitee/attendance fact exists for them anywhere (their
    Update modal is a dated list, not an event with real turnout).

    The query starts from `meeting_invitee`, not `leader`: some roles run to
    tens of thousands of members (Booth Convenor is ~44.6k) while an
    invitee list for one month is a few dozen, so starting from `leader` and
    left-joining spent the whole `MAX_PAGE_SIZE` cap on alphabetically-early
    members who were never invited to anything, and the real, non-zero rows
    fell past the limit unseen. Starting from the invitee list means every
    row returned has already been invited to at least one meeting this
    month — this branch drops the "one row per roster member" rule the other
    branch below keeps.
    """
    program_name = db.scalar(
        f"SELECT program_name FROM {config.PARTY_TRACK_DB}.program WHERE program_id = %s",
        (activity_id,),
    )
    is_calendar = _is_calendar_meetings(program_name)
    is_monthly = _is_monthly_activity(program_name)

    if is_calendar:
        rows = db.rows(
            f"""SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                       r.role_name, a.name AS assembly_name, p.parliament_name,
                       MAX(att.mid IS NOT NULL) AS attended
                  FROM meeting_invitee mi
                  JOIN {_leader_with_role_sql()} l ON l.tdp_cadre_id = mi.tdp_cadre_id
                  JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = l.role_id
                  LEFT JOIN assembly a ON a.id = l.constituency_id
                  LEFT JOIN parliament p ON p.id = l.parliament_id
                  LEFT JOIN (SELECT DISTINCT meeting_id, mid FROM meeting_attendance) att
                         ON att.meeting_id = CAST(mi.meeting_id AS UNSIGNED) AND att.mid = mi.membership_id
                 WHERE mi.meeting_id IN (
                         SELECT CAST(id AS CHAR) FROM meetings
                          WHERE YEAR(meeting_date) = %s AND MONTH(meeting_date) = %s
                       )
                   AND l.role_id = %s AND l.is_deleted = 'N' AND l.party_id = %s
                   AND {access.leader(scope)}
                 GROUP BY l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                          r.role_name, a.name, p.parliament_name
                 ORDER BY l.leader_name
                 LIMIT %s""",
            (year, month, role_id, config.LEADER_PARTY_ID, config.MAX_PAGE_SIZE),
        )
    elif is_monthly:
        # A real fact, off `mytdp.program`/`mytdp.program_attendance` — the
        # same domain Calendar Meetings reads, not `leader_program_activity`
        # (that table has no attendance concept of its own; see
        # `save_leader_monthly_activity`, which still owns the remark).
        # `pa.user_id` is the only column shared with `party_track.leader`
        # (as `tdp_cadre_id`), the same join shape `meeting_invitee` uses
        # above. See `_monthly_activity_mytdp_filter` for how `mp` is scoped
        # to this one activity.
        #
        # The attendance lookup is built as its own subquery, driven from
        # `program_attendance` through its indexed `program_id` — not joined
        # to `l` directly the way it reads most naturally. `user_id` carries
        # no index at all (495k rows, checked live), so a per-roster-row
        # lookup against it — the shape a direct `LEFT JOIN ... ON pa.user_id
        # = l.tdp_cadre_id` forces — timed out outright against the real
        # roster. Pre-filtering by the handful of `mp` rows this activity and
        # month actually match first keeps the expensive side small before it
        # ever meets the roster.
        mytdp_filter_sql, mytdp_filter_params = _monthly_activity_mytdp_filter(program_name)
        month_start, month_end = _month_bounds(year, month)
        rows = db.rows(
            f"""SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                       r.role_name, a.name AS assembly_name, p.parliament_name,
                       (att.user_id IS NOT NULL) AS attended
                  FROM {_leader_with_role_sql()} l
                  JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = l.role_id
                  LEFT JOIN assembly a ON a.id = l.constituency_id
                  LEFT JOIN parliament p ON p.id = l.parliament_id
                  LEFT JOIN (
                        SELECT DISTINCT pa.user_id
                          FROM program_attendance pa
                          JOIN program mp
                            ON pa.program_id = mp.id
                           AND {mytdp_filter_sql}
                           AND mp.from_date <= %s AND mp.to_date >= %s
                       ) att ON att.user_id = l.tdp_cadre_id
                 WHERE l.role_id = %s AND l.is_deleted = 'N' AND l.party_id = %s
                   AND {access.leader(scope)}
                 ORDER BY l.leader_name
                 LIMIT %s""",
            (*mytdp_filter_params, month_end, month_start, role_id, config.LEADER_PARTY_ID, config.MAX_PAGE_SIZE),
        )
    else:
        # No join to `leader_program_activity` here: with the counts gone
        # there is nothing on that row this card shows. What has been
        # recorded for one leader is fetched per-leader by the Update modal
        # (`program_leader_monthly_activity`), not smuggled into the roster.
        rows = db.rows(
            f"""SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                       r.role_name, a.name AS assembly_name, p.parliament_name
                  FROM {_leader_with_role_sql()} l
                  JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = l.role_id
                  LEFT JOIN assembly a ON a.id = l.constituency_id
                  LEFT JOIN parliament p ON p.id = l.parliament_id
                 WHERE l.role_id = %s AND l.is_deleted = 'N' AND l.party_id = %s
                   AND {access.leader(scope)}
                 ORDER BY l.leader_name
                 LIMIT %s""",
            (role_id, config.LEADER_PARTY_ID, config.MAX_PAGE_SIZE),
        )
    out = []
    for r in rows:
        leader = {
            "id": r["leader_id"],
            "mid": str(r["leader_id"]),
            "name": r["leader_name"] or "—",
            "role": r["role_name"] or "—",
            "parliament": r["parliament_name"] or "—",
            "assembly": r["assembly_name"] or "—",
            "mobile": r["mobile_no"] or "—",
            "cadreId": r["tdp_cadre_id"],
        }
        if is_calendar or is_monthly:
            leader["attended"] = bool(r["attended"])
        out.append(leader)
    return out


def _scoped_leader(leader_id: int, scope: Scope) -> dict[str, Any] | None:
    """This leader's row, or None when they sit outside the caller's granted
    assemblies — the gate every `/leaders/{leader_id}/…` route below goes
    through, so a leader the caller was never shown reads as unknown rather
    than as a refusal that confirms they exist."""
    return db.one(
        f"""SELECT l.tdp_cadre_id FROM {config.PARTY_TRACK_DB}.leader l
             WHERE l.leader_id = %s AND {access.leader(scope)}""",
        (leader_id,),
    )


def _leader_exists(leader_id: int) -> bool:
    """Not `_leader_cadre_id(...) is not None`: `tdp_cadre_id` is nullable, so
    that reads a real leader with no cadre id as an unknown one. Only the
    Calendar Meetings routes need the cadre id (it is their join key into
    `mytdp`); everything else keyed on `leader_id` just needs to know the
    leader is there."""
    return db.scalar(
        f"SELECT 1 FROM {config.PARTY_TRACK_DB}.leader WHERE leader_id = %s",
        (leader_id,),
    ) is not None


def _program_name(program_id: int) -> str | None:
    return db.scalar(
        f"SELECT program_name FROM {config.PARTY_TRACK_DB}.program WHERE program_id = %s",
        (program_id,),
    )


@router.get("/leaders/{leader_id}/meetings")
def program_leader_meetings(
    leader_id: int,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """Calendar Meetings' Update modal: every real committee meeting this
    leader (`leader_id` is `party_track.leader.leader_id`, the same `mid`
    `/leaders` returns for a calendar-variant row) was invited to in the
    given month — the individual `mytdp` rows behind their aggregate
    participated/completed count on the card above.

    `remarks`/`filePath` come from `party_track.leader_meeting_attendance`,
    the table this feature owns — not `mytdp.feedback_comment`, which only
    accepts remarks for absent invitees (a rule that belongs to the main
    Meetings screen, not here). See `save_leader_meeting_remarks` for the
    write side.
    """
    leader = _scoped_leader(leader_id, scope)
    if leader is None or leader["tdp_cadre_id"] is None:
        return []
    cadre_id = leader["tdp_cadre_id"]
    rows = db.rows(
        f"""SELECT m.id AS meeting_id, m.title, ml.level_name, m.meeting_date,
                   (att.mid IS NOT NULL) AS attended,
                   lma.remarks, lma.file_path
              FROM meeting_invitee mi
              JOIN meetings m ON m.id = CAST(mi.meeting_id AS UNSIGNED)
              LEFT JOIN meeting_levels ml ON ml.id = m.meeting_level_id
              LEFT JOIN (SELECT DISTINCT meeting_id, mid FROM meeting_attendance) att
                     ON att.meeting_id = m.id AND att.mid = mi.membership_id
              LEFT JOIN {config.PARTY_TRACK_DB}.leader_meeting_attendance lma
                     ON lma.leader_id = %s AND lma.meeting_id = m.id
                    AND (lma.is_deleted IS NULL OR lma.is_deleted = 'N')
             WHERE mi.tdp_cadre_id = %s
               AND YEAR(m.meeting_date) = %s AND MONTH(m.meeting_date) = %s
             ORDER BY m.meeting_date DESC""",
        (leader_id, cadre_id, year, month),
    )
    return [
        {
            "meetingId": r["meeting_id"],
            "meetingType": r["title"] or "—",
            "level": r["level_name"] or "",
            "date": r["meeting_date"].isoformat() if r["meeting_date"] else None,
            "attended": bool(r["attended"]),
            "remarks": r["remarks"] or "",
            "filePath": r["file_path"] or "",
        }
        for r in rows
    ]


class LeaderMeetingRemarksIn(BaseModel):
    remarks: str = Field(default="", max_length=config.MAX_REMARKS_CHARS)


def _leader_meeting_attendance_fact(leader_id: int, meeting_id: int, scope: Scope) -> int:
    """Confirms the leader was invited to this meeting (404s otherwise) and
    returns the real `attendance_type_id` for it, off the same
    `meeting_attendance` truth `program_leader_meetings` reads — not chosen
    by the caller, so it is re-derived here rather than passed in, and
    shared by the remarks-save and file-upload writes below: both stamp it
    on every write, alongside whichever one field is actually theirs, so it
    never goes stale on a row a later write only touches half of."""
    leader = _scoped_leader(leader_id, scope)
    if leader is None or leader["tdp_cadre_id"] is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    cadre_id = leader["tdp_cadre_id"]

    invitee = db.one(
        """SELECT membership_id FROM meeting_invitee
            WHERE tdp_cadre_id = %s AND meeting_id = %s LIMIT 1""",
        (cadre_id, str(meeting_id)),
    )
    if invitee is None:
        raise HTTPException(status_code=404, detail="Leader was not invited to this meeting")

    attended = db.scalar(
        "SELECT 1 FROM meeting_attendance WHERE meeting_id = %s AND mid = %s LIMIT 1",
        (meeting_id, invitee["membership_id"]),
    )
    return _ATTENDANCE_TYPE_ATTENDED if attended else _ATTENDANCE_TYPE_ABSENT


def _upsert_meeting_attendance_row(leader_id: int, meeting_id: int, attendance_type_id: int, **fields: Any) -> None:
    """Insert-or-update the one `leader_meeting_attendance` row for (leader,
    meeting), always refreshing `attendance_type_id` plus whichever other
    columns are given — a remarks-only write must not blank out a file
    already on the row, and a file-only write must not blank out a remark,
    the same reasoning `_upsert_monthly_activity_row` follows for the three
    monthly programmes."""
    existing = db.one(
        f"""SELECT leader_meeting_attendance_id
              FROM {config.PARTY_TRACK_DB}.leader_meeting_attendance
             WHERE leader_id = %s AND meeting_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')
             ORDER BY leader_meeting_attendance_id DESC LIMIT 1""",
        (leader_id, meeting_id),
    )
    if existing:
        set_sql = "attendance_type_id = %s, " + ", ".join(f"{col} = %s" for col in fields)
        db.execute(
            f"""UPDATE {config.PARTY_TRACK_DB}.leader_meeting_attendance
                   SET {set_sql}, updated_time = NOW()
                 WHERE leader_meeting_attendance_id = %s""",
            (attendance_type_id, *fields.values(), existing["leader_meeting_attendance_id"]),
        )
    else:
        cols = "leader_id, meeting_id, attendance_type_id, " + ", ".join(fields)
        placeholders = "%s, %s, %s, " + ", ".join(["%s"] * len(fields))
        db.execute(
            f"""INSERT INTO {config.PARTY_TRACK_DB}.leader_meeting_attendance
                   ({cols}, is_deleted, inserted_time)
                 VALUES ({placeholders}, 'N', NOW())""",
            (leader_id, meeting_id, attendance_type_id, *fields.values()),
        )


@router.put("/leaders/{leader_id}/meetings/{meeting_id}/remarks")
def save_leader_meeting_remarks(
    leader_id: int,
    meeting_id: int,
    body: LeaderMeetingRemarksIn = Body(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Calendar Meetings' own remarks capture, into `leader_meeting_attendance`
    rather than `mytdp.feedback_comment` — accepted regardless of whether the
    leader attended, unlike the main Meetings screen's absent-only rule. See
    `upload_leader_meeting_file` for the file half of this same row.
    """
    attendance_type_id = _leader_meeting_attendance_fact(leader_id, meeting_id, scope)
    remarks = body.remarks.strip()
    _upsert_meeting_attendance_row(leader_id, meeting_id, attendance_type_id, remarks=remarks)
    return {"leaderId": leader_id, "meetingId": meeting_id, "remarks": remarks}


@router.post("/leaders/{leader_id}/meetings/{meeting_id}/file")
async def upload_leader_meeting_file(
    leader_id: int,
    meeting_id: int,
    file: UploadFile = File(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Uploads one file for a leader's own row on a real Calendar Meeting,
    into S3 (`storage.upload`) with its `bucket/key` path saved onto the
    same `leader_meeting_attendance` row `save_leader_meeting_remarks`
    writes the remark to. Accepts the same file types every other upload
    field in this app's frontend already restricts its picker to (PDF or a
    common image type)."""
    attendance_type_id = _leader_meeting_attendance_fact(leader_id, meeting_id, scope)
    if file.content_type not in storage.ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF or image files are accepted.")

    content = await file.read()
    try:
        file_path = storage.upload(
            folder="calendar_meeting", content=content, content_type=file.content_type, filename=file.filename
        )
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not upload the file to storage.") from err

    _upsert_meeting_attendance_row(leader_id, meeting_id, attendance_type_id, file_path=file_path)
    return {"leaderId": leader_id, "meetingId": meeting_id, "filePath": file_path}


@router.get("/leaders/{leader_id}/meetings/{meeting_id}/file-url")
def leader_meeting_file_url(
    leader_id: int, meeting_id: int, scope: Scope = Depends(caller_scope)
) -> dict[str, Any]:
    """A fresh 5-minute link to the file `upload_leader_meeting_file` saved
    for this leader/meeting — generated per call rather than cached, same as
    the monthly activities' own file link, since the bucket refuses
    direct/public access."""
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    row = db.one(
        f"""SELECT file_path
              FROM {config.PARTY_TRACK_DB}.leader_meeting_attendance
             WHERE leader_id = %s AND meeting_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')
             ORDER BY leader_meeting_attendance_id DESC LIMIT 1""",
        (leader_id, meeting_id),
    )
    if row is None or not row["file_path"]:
        raise HTTPException(status_code=404, detail="No file recorded for this leader and meeting")
    try:
        url = storage.presigned_url(row["file_path"])
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not reach file storage.") from err
    return {"url": url}


def _monthly_activity_program(program_id: int) -> str:
    """The programme name behind a `leader_program_activity` route, refusing
    any programme that is not recorded that way rather than reading or
    writing the wrong table on its behalf."""
    program_name = _program_name(program_id)
    if program_name is None:
        raise HTTPException(status_code=404, detail="Unknown programme")
    if not _is_monthly_activity(program_name):
        raise HTTPException(
            status_code=400,
            detail=f"{program_name} is not recorded as a monthly activity",
        )
    return program_name


def _monthly_activity_row(leader_id: int, program_id: int, month_id: int | None):
    if month_id is None:
        return None
    return db.one(
        f"""SELECT leader_program_activity_id, remarks, file_path
              FROM {config.PARTY_TRACK_DB}.leader_program_activity
             WHERE leader_id = %s AND program_id = %s AND month_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')
             ORDER BY leader_program_activity_id DESC LIMIT 1""",
        (leader_id, program_id, month_id),
    )


def _upsert_monthly_activity_row(leader_id: int, program_id: int, month_id: int, **fields: Any) -> None:
    """Insert-or-update the one `leader_program_activity` row for (leader,
    programme, month), writing only the given columns — the remarks save and
    the file upload below both call this, and neither must blank out
    whatever the other one already wrote."""
    existing = _monthly_activity_row(leader_id, program_id, month_id)
    if existing:
        set_sql = ", ".join(f"{col} = %s" for col in fields)
        db.execute(
            f"""UPDATE {config.PARTY_TRACK_DB}.leader_program_activity
                   SET {set_sql}, updated_time = NOW()
                 WHERE leader_program_activity_id = %s""",
            (*fields.values(), existing["leader_program_activity_id"]),
        )
    else:
        cols = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        db.execute(
            f"""INSERT INTO {config.PARTY_TRACK_DB}.leader_program_activity
                   (leader_id, program_id, month_id, {cols}, is_deleted, inserted_time)
                 VALUES (%s, %s, %s, {placeholders}, 'N', NOW())""",
            (leader_id, program_id, month_id, *fields.values()),
        )


@router.get("/leaders/{leader_id}/monthly-activity")
def program_leader_monthly_activity(
    leader_id: int,
    program_id: int = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
) -> dict[str, Any]:
    """The Update modal for Pedala Sevalo, Swatch Andhra and Pattadar
    Passbook: this leader's single record for one programme and month, from
    `party_track.leader_program_activity`.

    One record, not a list, because that table keys a row by `month_id` and
    carries no date column — there is nowhere to file a second row for the
    same leader, programme and month that a reader could tell apart from the
    first. `recorded` is what separates "nothing has been entered yet" from
    "entered, and the remark is empty", which the two render differently;
    absent a `month` row for the period there is nothing to find either way,
    so that reads as not recorded rather than as an error (`_month_id`).
    """
    _monthly_activity_program(program_id)
    row = _monthly_activity_row(leader_id, program_id, _month_id(year, month))
    return {
        "leaderId": leader_id,
        "programId": program_id,
        "recorded": row is not None,
        "remarks": (row["remarks"] if row else None) or "",
        "filePath": (row["file_path"] if row else None) or "",
    }


class LeaderMonthlyActivityIn(BaseModel):
    programId: int
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    remarks: str = Field(default="", max_length=config.MAX_REMARKS_CHARS)


@router.put("/leaders/{leader_id}/monthly-activity")
def save_leader_monthly_activity(
    leader_id: int, body: LeaderMonthlyActivityIn = Body(...)
) -> dict[str, Any]:
    """Records one leader's month for one of the three monthly programmes,
    into `leader_program_activity` — the same table the Updated/Not updated
    figures on both summary cards are counted from, so a save here is what
    moves them.

    An upsert on (leader, programme, month) rather than an insert: the row
    *is* the month, so saving twice corrects the record rather than adding a
    second one — `_upsert_monthly_activity_row` owns that, writing only
    `remarks` so a file already on this row from
    `upload_leader_monthly_activity_file` survives a later remarks-only
    save. `total`/`completed` are left alone — nothing writes them any
    more, and the card that used to read them no longer does (see
    `program_leaders`).

    A period with no `party_track.month` row is refused rather than
    invented. That table is hand-seeded and runs a month or two behind, so
    this is the error the Programmes screen hits first each time it laps the
    seed — the detail names the period so it is clear what to add, and no
    `month` row is created here on the guess that it would have looked like
    its neighbours.
    """
    _monthly_activity_program(body.programId)
    if not _leader_exists(leader_id):
        raise HTTPException(status_code=404, detail="Unknown leader")

    month_id = _month_id(body.year, body.month)
    if month_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"{body.year}-{body.month:02d} is not open for reporting "
                   f"({config.PARTY_TRACK_DB}.month has no row for it)",
        )

    remarks = body.remarks.strip()
    _upsert_monthly_activity_row(leader_id, body.programId, month_id, remarks=remarks)
    return {
        "leaderId": leader_id,
        "programId": body.programId,
        "recorded": True,
        "remarks": remarks,
    }


@router.post("/leaders/{leader_id}/monthly-activity/file")
async def upload_leader_monthly_activity_file(
    leader_id: int,
    program_id: int = Form(...),
    year: int = Form(..., ge=2000, le=2100),
    month: int = Form(..., ge=1, le=12),
    file: UploadFile = File(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Uploads one file for a leader's month on one of the three monthly
    activities, into S3 (`storage.upload`), and records its `bucket/key` path
    on the same `leader_program_activity` row `save_leader_monthly_activity`
    writes the remark to — via `_upsert_monthly_activity_row`, so whichever
    of the two is saved first does not erase the other.

    Accepts the same file types every other upload field in this app's
    frontend already restricts its picker to (PDF or a common image type),
    not the portal's PDF-only nomination rule — this is a proof-of-activity
    photo/document, not a nomination form.
    """
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    _monthly_activity_program(program_id)
    if file.content_type not in storage.ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF or image files are accepted.")

    month_id = _month_id(year, month)
    if month_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"{year}-{month:02d} is not open for reporting "
                   f"({config.PARTY_TRACK_DB}.month has no row for it)",
        )

    content = await file.read()
    try:
        file_path = storage.upload(
            folder="monthly_activity", content=content, content_type=file.content_type, filename=file.filename
        )
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not upload the file to storage.") from err

    _upsert_monthly_activity_row(leader_id, program_id, month_id, file_path=file_path)
    return {"leaderId": leader_id, "programId": program_id, "filePath": file_path}


@router.get("/leaders/{leader_id}/monthly-activity/file-url")
def leader_monthly_activity_file_url(
    leader_id: int,
    program_id: int = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """A fresh 5-minute link to the file `upload_leader_monthly_activity_file`
    saved for this leader/programme/month — generated per call rather than
    cached, same as the portal's own nomination-file link, since the bucket
    itself refuses direct/public access."""
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    _monthly_activity_program(program_id)
    row = _monthly_activity_row(leader_id, program_id, _month_id(year, month))
    if row is None or not row["file_path"]:
        raise HTTPException(status_code=404, detail="No file recorded for this leader, programme and month")
    try:
        url = storage.presigned_url(row["file_path"])
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not reach file storage.") from err
    return {"url": url}


# `party_track.leader_meetings.meeting_type` is `varchar(20)` — too short for
# some of `party_track.program`'s own names, so those are stored under a
# short label instead (confirmed against the live `program` rows: id 3
# Grievance Meetings, 4 Cadre Meetings, 5 Central Party Office Grievance, 8
# PC Lunch/Dinner Meetings, 9 Press Meets). Field Performance already fits
# (<=20 chars) and is stored verbatim. Two groups of programmes are
# deliberately absent: Calendar Meeting, which writes to
# `leader_meeting_attendance`, and `_MONTHLY_ACTIVITY_PROGRAMS` (Pedala
# Sevalo, Swatch Andhra, Pattadar Passbook), which write to
# `leader_program_activity` — none of the five reaches this table at all, and
# the routes below refuse them rather than labelling them.
_MEETING_TYPE_LABELS = {
    "grievance meetings": "Grievance",
    "cadre meetings": "Cadre",
    "central party office grievance": "Office Grievance",
    "pc lunch/dinner meetings": "Dinner",
    "press meets": "Pressmeet",
}


def _meeting_type_label(program_name: str) -> str:
    name = program_name.strip()
    return _MEETING_TYPE_LABELS.get(name.lower(), name)[:20]


def _log_entry_program(program_id: int) -> str | None:
    """The programme name behind a `leader_meetings` route.

    `None` for an unknown programme — the read below has always treated that
    as "no entries" rather than an error. A programme recorded in
    `leader_program_activity` instead is a different matter and raises: a
    caller asking `leader_meetings` for Pedala Sevalo would otherwise be told
    "no entries yet" for data that exists in another table, which is exactly
    the reading a silent empty list invites.
    """
    program_name = _program_name(program_id)
    if program_name is None:
        return None
    if _is_monthly_activity(program_name):
        raise HTTPException(
            status_code=400,
            detail=f"{program_name} is recorded as a monthly activity — "
                   "use the monthly-activity endpoint instead",
        )
    return program_name


@router.get("/leaders/{leader_id}/log-entries")
def program_leader_log_entries(
    leader_id: int,
    program_id: int = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """The Update modal for the programmes that keep a dated log — every one
    except Calendar Meetings and `_MONTHLY_ACTIVITY_PROGRAMS`: a leader's own
    hand-added date/remarks entries for one programme/month, off
    `party_track.leader_meetings`. A manually-added list, unlike Calendar
    Meetings' real invitee-backed rows, so there is nothing to distinguish
    from "not invited" here, only what has been logged so far.

    Scoped by `meeting_type` (see `_meeting_type_label`) the same way it is
    written on save, so switching the Update modal between two programmes
    for the same leader never shows one programme's entries under another's.
    """
    if _scoped_leader(leader_id, scope) is None:
        return []
    program_name = _log_entry_program(program_id)
    if program_name is None:
        return []
    meeting_type = _meeting_type_label(program_name)
    rows = db.rows(
        f"""SELECT leader_meetings_id, meeting_date, remarks, file_path
              FROM {config.PARTY_TRACK_DB}.leader_meetings
             WHERE leader_id = %s AND meeting_type = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')
               AND YEAR(meeting_date) = %s AND MONTH(meeting_date) = %s
             ORDER BY meeting_date DESC, leader_meetings_id DESC""",
        (leader_id, meeting_type, year, month),
    )
    return [
        {
            "id": r["leader_meetings_id"],
            "date": r["meeting_date"].isoformat() if r["meeting_date"] else None,
            "remarks": r["remarks"] or "",
            "filePath": r["file_path"] or "",
        }
        for r in rows
    ]


class LeaderLogEntryIn(BaseModel):
    programId: int
    date: str
    remarks: str = Field(default="", max_length=config.MAX_REMARKS_CHARS)


@router.post("/leaders/{leader_id}/log-entries")
def add_leader_log_entry(
    leader_id: int,
    body: LeaderLogEntryIn = Body(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Adds one row to a leader's log for one programme. `file_path` is left
    unset here — a file goes on afterwards, by this row's own id, through
    `upload_leader_log_entry_file`, not through this call.
    """
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    program_name = _log_entry_program(body.programId)
    if program_name is None:
        raise HTTPException(status_code=404, detail="Unknown programme")
    meeting_type = _meeting_type_label(program_name)
    remarks = body.remarks.strip()
    new_id = db.insert(
        f"""INSERT INTO {config.PARTY_TRACK_DB}.leader_meetings
               (leader_id, meeting_type, meeting_date, remarks, is_deleted, inserted_time)
             VALUES (%s, %s, %s, %s, 'N', NOW())""",
        (leader_id, meeting_type, body.date, remarks),
    )
    return {"id": new_id, "date": body.date, "remarks": remarks}


@router.post("/leaders/{leader_id}/log-entries/{entry_id}/file")
async def upload_leader_log_entry_file(
    leader_id: int,
    entry_id: int,
    file: UploadFile = File(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Uploads one file onto an existing log entry — `add_leader_log_entry`
    already created the row, so this only ever updates `file_path` on the
    one row `entry_id` names, into S3 (`storage.upload`). Unlike
    `leader_meeting_attendance`/`leader_program_activity`, there is no
    upsert here: a log entry with no matching row is a bad id, not a row to
    invent."""
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    if file.content_type not in storage.ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF or image files are accepted.")

    content = await file.read()
    try:
        file_path = storage.upload(
            folder="log_entry", content=content, content_type=file.content_type, filename=file.filename
        )
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not upload the file to storage.") from err

    updated = db.execute(
        f"""UPDATE {config.PARTY_TRACK_DB}.leader_meetings
               SET file_path = %s, updated_time = NOW()
             WHERE leader_meetings_id = %s AND leader_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')""",
        (file_path, entry_id, leader_id),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"id": entry_id, "filePath": file_path}


@router.get("/leaders/{leader_id}/log-entries/{entry_id}/file-url")
def leader_log_entry_file_url(
    leader_id: int, entry_id: int, scope: Scope = Depends(caller_scope)
) -> dict[str, Any]:
    """A fresh 5-minute link to the file `upload_leader_log_entry_file`
    saved for this entry — generated per call rather than cached, same as
    every other file link in this router, since the bucket refuses
    direct/public access."""
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    row = db.one(
        f"""SELECT file_path
              FROM {config.PARTY_TRACK_DB}.leader_meetings
             WHERE leader_meetings_id = %s AND leader_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')""",
        (entry_id, leader_id),
    )
    if row is None or not row["file_path"]:
        raise HTTPException(status_code=404, detail="No file recorded for this entry")
    try:
        url = storage.presigned_url(row["file_path"])
    except storage.StorageUnavailable as err:
        raise HTTPException(status_code=503, detail="File storage is not configured on the server.") from err
    except storage.UploadFailed as err:
        raise HTTPException(status_code=502, detail="Could not reach file storage.") from err
    return {"url": url}


@router.delete("/leaders/{leader_id}/log-entries/{entry_id}")
def delete_leader_log_entry(
    leader_id: int, entry_id: int, scope: Scope = Depends(caller_scope)
) -> dict[str, Any]:
    """Soft-deletes one log row — `is_deleted`, not a real DELETE, matching
    every other removal in this schema (`leader_meeting_attendance`
    included)."""
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    updated = db.execute(
        f"""UPDATE {config.PARTY_TRACK_DB}.leader_meetings
               SET is_deleted = 'Y', updated_time = NOW()
             WHERE leader_meetings_id = %s AND leader_id = %s""",
        (entry_id, leader_id),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"id": entry_id}


@router.get("/{program_id}/daywise")
async def day_wise(program_id: str) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail=_UNWIRED)


@router.get("/{program_id}/attendees")
async def attendees(
    program_id: str,
    ac: str | None = Query(None, description="Constituency name, exact match"),
    limit: int = Query(config.MAX_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail=_UNWIRED)
