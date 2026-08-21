"""Program meetings, and the Programmes page's role/activity/leader tracking.

The `/{program_id}/...` routes below are a different, still-unwired feature
(individual programme *events*, mirroring the `meetings` domain) — their
contract survives the removal of upstream wiring, the bodies do not.

The role/activity-summary/leaders routes are real and live off four
`party_track` tables: `role` (already populated, shared with `/roles`),
`leader` (the 71k-row real roster), `program` and `program_role` (the
catalog of trackable programmes and which roles they apply to), and
`leader_program_activity` (one row per leader/programme/month once someone
records participation). The last three are freshly added and still empty in
production, so Updated/Not updated and the whole activity-summary card read
zero/empty until the party starts populating them — Total/Members do not,
since those come straight off the `leader` roster.

`party_track.activity` is a separate, long-lived scoring system
(`leader_activity`, ~800k rows, weighted points) unrelated to this feature.
`/activities` below now serves `party_track.program` instead — the
Programmes page's own "Activity" column and filter are `program`, since
`leader_program_activity.program_id` is what it actually joins against.

Calendar Meetings is the one programme `/leaders` does not read
`leader_program_activity` for: it counts real committee-meeting attendance
from `mytdp.meetings`/`meeting_invitee`/`meeting_attendance` instead — see
`_is_calendar_meetings` and the branch in `program_leaders`.

Every roster count and member list here is narrowed to the assemblies the caller
was granted, through `leader.constituency_id` — the same id `mytdp.assembly.id`
uses (see `access.py`). A role with no leader inside the caller's assemblies
drops out of `/roles` and both summary cards entirely, rather than showing as a
row of zeroes.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import access, config, db
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


def _is_calendar_meetings(program_name: str | None) -> bool:
    return bool(program_name and _CALENDAR_MEETINGS_RE.search(program_name))


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
    """The third Programmes card: every active leader in one role, with
    their `leader_program_activity` participation for one programme and
    month — 0/0 for a leader who has not logged anything yet, not an
    absent row, so the card always has one row per roster member.

    `assembly`/`parliament` are `mytdp` tables, not `party_track` —
    `leader.constituency_id`/`parliament_id` reach across schemas the same
    way `config.PARTY_TRACK_DB`'s own docstring says the RDS user allows.
    `mobile`/`cadreId` are `leader.mobile_no`/`tdp_cadre_id` — plain columns
    on the same row, no further join needed. Scoped to `config.LEADER_PARTY_ID`
    — `leader` holds a handful of rows for other parties. Capped at
    `MAX_PAGE_SIZE` for the same reason the meeting member table is.

    Calendar Meetings (see `_is_calendar_meetings`) is the exception: it
    counts real `mytdp` committee-meeting rows instead of
    `leader_program_activity`, which this programme never writes to.
    `meeting_invitee.tdp_cadre_id` is the only column shared with
    `party_track.leader`, so that is the join key; `participated` is every
    meeting this leader was invited to in the month, `completed` the ones
    they attended — any row for them in `meeting_attendance`, whatever its
    `status`, the same "attended" definition `/api/meetings` itself uses.

    The query starts from `meeting_invitee`, not `leader`: some roles run to
    tens of thousands of members (Booth Convenor is ~44.6k) while an
    invitee list for one month is a few dozen, so starting from `leader` and
    left-joining spent the whole `MAX_PAGE_SIZE` cap on alphabetically-early
    members who were never invited to anything, and the real, non-zero rows
    fell past the limit unseen. Starting from the invitee list means every
    row returned has already been invited to at least one meeting this
    month — this branch drops the "every roster member, 0/0 if none" rule
    the `leader_program_activity` branch below keeps.
    """
    month_id = _month_id(year, month)
    program_name = db.scalar(
        f"SELECT program_name FROM {config.PARTY_TRACK_DB}.program WHERE program_id = %s",
        (activity_id,),
    )

    if _is_calendar_meetings(program_name):
        rows = db.rows(
            f"""SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                       r.role_name, a.name AS assembly_name, p.parliament_name,
                       COUNT(DISTINCT mi.meeting_id) AS participated,
                       COUNT(DISTINCT CASE WHEN att.mid IS NOT NULL THEN mi.meeting_id END) AS completed
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
    else:
        rows = db.rows(
            f"""SELECT l.leader_id, l.leader_name, l.mobile_no, l.tdp_cadre_id,
                       r.role_name, a.name AS assembly_name, p.parliament_name,
                       lpa.total AS participated, lpa.completed AS completed
                  FROM {_leader_with_role_sql()} l
                  JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = l.role_id
                  LEFT JOIN assembly a ON a.id = l.constituency_id
                  LEFT JOIN parliament p ON p.id = l.parliament_id
                  LEFT JOIN {config.PARTY_TRACK_DB}.leader_program_activity lpa
                         ON lpa.leader_id = l.leader_id AND lpa.program_id = %s AND lpa.month_id = %s
                        AND (lpa.is_deleted IS NULL OR lpa.is_deleted = 'N')
                 WHERE l.role_id = %s AND l.is_deleted = 'N' AND l.party_id = %s
                   AND {access.leader(scope)}
                 ORDER BY l.leader_name
                 LIMIT %s""",
            (activity_id, month_id, role_id, config.LEADER_PARTY_ID, config.MAX_PAGE_SIZE),
        )
    return [
        {
            "id": r["leader_id"],
            "mid": str(r["leader_id"]),
            "name": r["leader_name"] or "—",
            "role": r["role_name"] or "—",
            "parliament": r["parliament_name"] or "—",
            "assembly": r["assembly_name"] or "—",
            "mobile": r["mobile_no"] or "—",
            "cadreId": r["tdp_cadre_id"],
            "participated": int(r["participated"] or 0),
            "completed": int(r["completed"] or 0),
        }
        for r in rows
    ]


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


@router.put("/leaders/{leader_id}/meetings/{meeting_id}/remarks")
def save_leader_meeting_remarks(
    leader_id: int,
    meeting_id: int,
    body: LeaderMeetingRemarksIn = Body(...),
    scope: Scope = Depends(caller_scope),
) -> dict[str, Any]:
    """Calendar Meetings' own remarks capture, into `leader_meeting_attendance`
    rather than `mytdp.feedback_comment` — accepted regardless of whether the
    leader attended, unlike the main Meetings screen's absent-only rule.

    `attendance_type_id` is stamped from the same `meeting_attendance` truth
    `program_leader_meetings` reads (Attended/Absent), not chosen by the
    caller — it records what actually happened, alongside the remark, rather
    than trusting the client to say so. `file_path` is left untouched here:
    there is no upload endpoint yet to have written one.
    """
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
    attendance_type_id = _ATTENDANCE_TYPE_ATTENDED if attended else _ATTENDANCE_TYPE_ABSENT
    remarks = body.remarks.strip()

    existing = db.one(
        f"""SELECT leader_meeting_attendance_id
              FROM {config.PARTY_TRACK_DB}.leader_meeting_attendance
             WHERE leader_id = %s AND meeting_id = %s
               AND (is_deleted IS NULL OR is_deleted = 'N')
             ORDER BY leader_meeting_attendance_id DESC LIMIT 1""",
        (leader_id, meeting_id),
    )
    if existing:
        db.execute(
            f"""UPDATE {config.PARTY_TRACK_DB}.leader_meeting_attendance
                   SET remarks = %s, attendance_type_id = %s, updated_time = NOW()
                 WHERE leader_meeting_attendance_id = %s""",
            (remarks, attendance_type_id, existing["leader_meeting_attendance_id"]),
        )
    else:
        db.execute(
            f"""INSERT INTO {config.PARTY_TRACK_DB}.leader_meeting_attendance
                   (leader_id, meeting_id, attendance_type_id, remarks, is_deleted, inserted_time)
                 VALUES (%s, %s, %s, %s, 'N', NOW())""",
            (leader_id, meeting_id, attendance_type_id, remarks),
        )
    return {"leaderId": leader_id, "meetingId": meeting_id, "remarks": remarks}


# `party_track.leader_meetings.meeting_type` is `varchar(20)` — too short for
# some of `party_track.program`'s own names, so those are stored under a
# short label instead (confirmed against the live `program` rows: id 3
# Grievance Meetings, 4 Cadre Meetings, 5 Central Party Office Grievance, 8
# PC Lunch/Dinner Meetings, 9 Press Meets). Everything else already fits
# (Pedala Sevalo, Swatch Andhra, Pattadar Passbook, Field Performance, all
# <=20 chars) and is stored verbatim. Calendar Meeting is deliberately absent
# here — it never writes to this table, see `leader_meeting_attendance` above.
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


def _program_name(program_id: int) -> str | None:
    return db.scalar(
        f"SELECT program_name FROM {config.PARTY_TRACK_DB}.program WHERE program_id = %s",
        (program_id,),
    )


@router.get("/leaders/{leader_id}/log-entries")
def program_leader_log_entries(
    leader_id: int,
    program_id: int = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    scope: Scope = Depends(caller_scope),
) -> list[dict[str, Any]]:
    """The Update modal for every programme other than Calendar Meetings: a
    leader's own hand-added date/remarks log for one programme/month, off
    `party_track.leader_meetings` — a manually-added list, unlike Calendar
    Meetings' real invitee-backed rows, so there is nothing to distinguish
    from "not invited" here, only what has been logged so far.

    Scoped by `meeting_type` (see `_meeting_type_label`) the same way it is
    written on save, so switching the Update modal between two programmes
    for the same leader never shows one programme's entries under another's.
    """
    if _scoped_leader(leader_id, scope) is None:
        return []
    program_name = _program_name(program_id)
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
    unset here, same as `save_leader_meeting_remarks` above: there is no
    upload endpoint yet to have written one.
    """
    if _scoped_leader(leader_id, scope) is None:
        raise HTTPException(status_code=404, detail="Unknown leader")
    program_name = _program_name(body.programId)
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
