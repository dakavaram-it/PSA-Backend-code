"""Committee meetings — the meeting list, its invitees, and remark capture.

Three facts about `mytdp` shape this file:

* **Attendance does not partition the invitee list.** `meeting_attendance` is one
  row per record and mostly belongs to people who were never invited, so an
  invitee counts as attended only when their `membership_id` appears there, and
  the join is against a de-duplicated set — joining the raw table multiplies an
  invitee by their attendance rows and inflates every count.
* **`meeting_invitee.meeting_id` is a varchar, `meeting_attendance.meeting_id` a
  bigint.** The cast goes on the invitee side so the attendance index still bites.
* **Nothing is cached.** Every figure is counted in SQL at request time; the
  Unit-level meeting is 167k invitees and still answers in seconds, which is what
  the old pull-once-into-SQLite projection existed to avoid.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from .. import adapt, config, db
from .committees import _LOCATIONS as _COMMITTEE_LOCATIONS

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


class RemarksIn(BaseModel):
    remarks: str = Field(default="", max_length=config.MAX_REMARKS_CHARS)
    capturedBy: str = Field(default="", max_length=64)


_MEETING_COLS = """
SELECT m.id, m.title, m.meeting_date, l.level_name
  FROM meetings m
  LEFT JOIN meeting_levels l ON l.id = m.meeting_level_id
"""

# One attendance row per (meeting, member): the raw table repeats a member and
# would multiply their invitee row in every join below.
_ATTENDED = "(SELECT DISTINCT meeting_id, mid FROM meeting_attendance)"

# A capture is a live, non-empty remark on a committee meeting.
_FEEDBACK = f"""
  FROM feedback_comment
 WHERE feeback_program_type_id = {config.MEETING_TYPE_ID}
   AND COALESCE(is_deleted, 'N') <> 'Y'
   AND remarks IS NOT NULL AND remarks <> ''
"""


# The six light counts, as one statement. Each still scans only its own table
# and groups the same way it did as six separate queries; they are stitched with
# UNION ALL because the round trip, not the scan, is what a dashboard load pays
# for — ~200ms each from outside the VPC, six of them for figures that take
# milliseconds to count. `src` says which count a row carries and the three
# value columns are read positionally through `_LIGHT_KEYS`.
_LIGHT_AGGREGATES = """
SELECT 'attendance' AS src, meeting_id AS id, COUNT(*) AS a, 0 AS b, 0 AS c
  FROM meeting_attendance WHERE meeting_id IN ({marks}) GROUP BY meeting_id
UNION ALL
SELECT 'schedules', meeting_id, COUNT(*), SUM(status IN (1, 2)), 0
  FROM meeting_schedules WHERE meeting_id IN ({marks}) GROUP BY meeting_id
UNION ALL
SELECT 'resolutions', meeting_id, COUNT(*), 0, 0
  FROM meeting_resolutions WHERE meeting_id IN ({marks}) GROUP BY meeting_id
UNION ALL
SELECT 'pc', meeting_id, COUNT(*), SUM(is_conducted = 'Y'), SUM(is_conducted IS NULL)
  FROM meeting_conducted_status WHERE meeting_id IN ({marks}) GROUP BY meeting_id
UNION ALL
SELECT 'pcRemarks', meeting_id, COUNT(*), 0, 0
  FROM meeting_remark
 WHERE meeting_id IN ({marks}) AND remarks IS NOT NULL AND remarks <> ''
 GROUP BY meeting_id
UNION ALL
SELECT 'feedback', program_id, COUNT(*), 0, 0 {feedback} AND program_id IN ({marks})
 GROUP BY program_id
"""

# Which figure each `src` row carries, in column order. `pcTotal`/`pcConducted`/
# `pcNull` keep their old meanings: 'Y' alone counts as conducted, and NULL is
# kept apart from an explicit 'N' because "App & PC Not updated" means NULL.
_LIGHT_KEYS = {
    "attendance": ("attendanceRecords",),
    "schedules": ("units", "unitsCompleted"),
    "resolutions": ("resolutions",),
    "pc": ("pcTotal", "pcConducted", "pcNull"),
    "pcRemarks": ("pcRemarks",),
    "feedback": ("feedbackTaken",),
}


def _aggregates(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Every counted figure for these meetings, keyed by meeting id.

    Two queries: the invitee list against attendance, which is the only slow one
    (half a million invitee rows, seconds), and everything else in one pass.
    """
    if not ids:
        return {}
    marks = db.placeholders(ids)
    agg: dict[str, dict[str, Any]] = {i: {} for i in ids}

    for r in db.rows(f"""
        SELECT i.meeting_id, COUNT(*) AS invitees,
               SUM(a.mid IS NOT NULL) AS attendees
          FROM meeting_invitee i
          LEFT JOIN {_ATTENDED} a
                 ON a.meeting_id = CAST(i.meeting_id AS UNSIGNED)
                AND a.mid = i.membership_id
         WHERE i.meeting_id IN ({marks})
         GROUP BY i.meeting_id""", tuple(ids)):
        agg[str(r["meeting_id"])].update(invitees=r["invitees"], attendees=r["attendees"])

    sql = _LIGHT_AGGREGATES.format(marks=marks, feedback=_FEEDBACK)
    for r in db.rows(sql, tuple(ids) * 6):
        slot = agg.get(str(r["id"]))
        if slot is None:
            continue
        slot.update(zip(_LIGHT_KEYS[r["src"]], (r["a"], r["b"], r["c"])))

    return agg


# One statement, not three: the three roster sizes are trivial to count and were
# costing a round trip each (see db.py — a round trip is ~200ms from outside the
# VPC). Only the levels a meeting list actually holds are asked for.
# Every branch names its own columns: a UNION takes them from whichever branch
# comes first, and the first one here depends on which levels the meeting list
# holds — aliasing only one of them broke any list without a Unit meeting.
_ROSTER_SIZES = {
    "Unit": "SELECT 'Unit' AS level, COUNT(DISTINCT unit_id) AS n FROM booth WHERE publication_id = %s",
    "AC": "SELECT 'AC' AS level, COUNT(*) AS n FROM assembly",
    "PC": "SELECT 'PC' AS level, COUNT(*) AS n FROM parliament",
}

# How each level's `meeting_schedules.entity_id` resolves to a roster row. The
# cast stays on the schedules side so the target table's own key still indexes
# (same reason as the invitee/attendance join above).
_ROSTER_JOINS = {
    "Unit": ("""JOIN unit u ON u.id = CAST(s.entity_id AS CHAR)
                JOIN booth b ON b.unit_id = u.id AND b.publication_id = %s""", True),
    "AC": ("JOIN assembly a ON a.id = CAST(s.entity_id AS CHAR)", False),
    "PC": ("JOIN parliament p ON p.id = CAST(s.entity_id AS CHAR)", False),
}


def _matched_counts(by_level: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Per level and meeting id: how many *distinct* roster locations that
    meeting's own `meeting_schedules` rows land on, via joins the database can
    index rather than a roster fetched whole into Python and diffed row by row.

    All the levels present go in one UNION ALL for the same reason the light
    aggregates do — three of these were three round trips for work that takes
    milliseconds once the query arrives.
    """
    parts, args = [], []
    for level, ids in by_level.items():
        join, needs_publication = _ROSTER_JOINS[level]
        parts.append(f"""
            SELECT '{level}' AS level, s.meeting_id AS id, COUNT(DISTINCT s.entity_id) AS matched
              FROM meeting_schedules s
              {join}
             WHERE s.meeting_id IN ({db.placeholders(ids)})
             GROUP BY s.meeting_id""")
        if needs_publication:
            args.append(config.UNIT_PUBLICATION_ID)
        args.extend(ids)

    out: dict[str, dict[str, int]] = {level: {} for level in by_level}
    for r in db.rows(" UNION ALL ".join(parts), tuple(args)):
        out[r["level"]][str(r["id"])] = r["matched"]
    return out


def _not_scheduled_counts(meeting_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Per meeting: how many of its level's roster locations have no
    `meeting_schedules` row at all — the same figure `/schedules/not-scheduled`
    drills down to rows, computed the same way so the two always foot to each
    other.

    Not a flat "roster size minus this meeting's own schedule count": a
    meeting can carry a schedule row whose `entity_id` falls outside the
    roster (a mandal committee scheduled but not currently enrolled, say),
    and subtracting the raw count would silently undercount `notScheduled` by
    however many of those a meeting happens to have. Only the roster ids this
    meeting's `entity_id`s actually cover get subtracted instead — as a join
    and a `COUNT(DISTINCT …)` for Unit/AC/PC, whose rosters live in `mytdp`
    right next to `meeting_schedules`. Fetching the ~8.7k-row Unit roster
    into Python to diff it there, as this used to, cost several seconds on
    every meetings-list load for no reason the database can't do itself.

    Mandal is the exception: its roster lives in `dakavara_pa`, a different
    schema with no shared id space to join on, so that one id set still has
    to come across and get diffed in Python — cheap on its own (936 rows).
    """
    by_level: dict[str, list[str]] = {}
    for r in meeting_rows:
        by_level.setdefault(adapt.level_code(r["level_name"]), []).append(str(r["id"]))

    out: dict[str, int] = {}

    joined = {lvl: ids for lvl, ids in by_level.items() if lvl in _ROSTER_JOINS}
    if joined:
        sizes_sql, sizes_args = [], []
        for level in joined:
            sizes_sql.append(_ROSTER_SIZES[level])
            if level == "Unit":
                sizes_args.append(config.UNIT_PUBLICATION_ID)
        sizes = {
            r["level"]: r["n"]
            for r in db.rows(" UNION ALL ".join(sizes_sql), tuple(sizes_args))
        }
        matched = _matched_counts(joined)
        for level, ids in joined.items():
            for mid in ids:
                out[mid] = max(sizes.get(level, 0) - matched[level].get(mid, 0), 0)

    mandal_ids = by_level.get("Mandal", [])
    if mandal_ids:
        marks = db.placeholders(mandal_ids)
        roster_ids = {str(r["location_id"]) for r in db.rows(_COMMITTEE_LOCATIONS) if r["location_id"] is not None}
        scheduled_by_meeting: dict[str, set[str]] = {}
        for r in db.rows(
            f"SELECT meeting_id, entity_id FROM meeting_schedules WHERE meeting_id IN ({marks})",
            tuple(mandal_ids),
        ):
            scheduled_by_meeting.setdefault(str(r["meeting_id"]), set()).add(str(r["entity_id"]))
        for mid in mandal_ids:
            matched_mandal = len(roster_ids & scheduled_by_meeting.get(mid, set()))
            out[mid] = max(len(roster_ids) - matched_mandal, 0)

    return out


@router.get("")
def list_meetings(
    from_date: str = Query(..., alias="from", description="YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="YYYY-MM-DD"),
) -> list[dict[str, Any]]:
    """Committee meetings held in a period, oldest first."""
    rows = db.rows(
        _MEETING_COLS + """
         WHERE m.meeting_type_id = %s AND m.meeting_date BETWEEN %s AND %s
         ORDER BY m.meeting_date""",
        (config.MEETING_TYPE_ID, from_date, to_date),
    )
    # Neither group needs the other's answer and both are seconds of database
    # work, so they overlap rather than queue.
    agg, not_scheduled = db.parallel(
        lambda: _aggregates([str(r["id"]) for r in rows]),
        lambda: _not_scheduled_counts(rows),
    )
    return [
        adapt.meeting(r, agg.get(str(r["id"]), {}), not_scheduled.get(str(r["id"]), 0))
        for r in rows
    ]


def _ids(meeting_ids: str) -> list[str]:
    return [i for i in meeting_ids.split(",") if i]


def _schedule_rows(meeting_ids: str, condition: str) -> dict[str, Any]:
    """Row-level `meeting_schedules` detail behind an App-side figure."""
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": []}
    marks = db.placeholders(ids)
    rows = db.rows(
        f"""SELECT id, meeting_id, location_text, meeting_time
              FROM meeting_schedules
             WHERE meeting_id IN ({marks}) AND {condition}
             ORDER BY meeting_id, id""",
        tuple(ids),
    )
    return {
        "total": len(rows),
        "rows": [
            {
                "id": r["id"],
                "meetingId": str(r["meeting_id"]),
                "location": r["location_text"] or "",
                "time": r["meeting_time"] or "",
            }
            for r in rows
        ],
    }


def _conducted_status_rows(meeting_ids: str, condition: str) -> dict[str, Any]:
    """Row-level `meeting_conducted_status` detail behind a PC-side figure.

    `role`/`unit` are looked up for a readable row; a location outside `unit`
    (a level other than Unit) falls back to the raw id rather than dropping
    the row, since no other level has real data yet to confirm its join.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": []}
    marks = db.placeholders(ids)
    rows = db.rows(
        f"""SELECT mcs.meeting_conducted_status_id AS id, mcs.meeting_id,
                   r.code AS role_code, COALESCE(u.code, mcs.location_id) AS location_code
              FROM meeting_conducted_status mcs
              LEFT JOIN role r ON r.id = mcs.role_id
              LEFT JOIN unit u ON u.id = mcs.location_id
             WHERE mcs.meeting_id IN ({marks}) AND {condition}
             ORDER BY mcs.meeting_id, mcs.meeting_conducted_status_id""",
        tuple(ids),
    )
    return {
        "total": len(rows),
        "rows": [
            {
                "id": r["id"],
                "meetingId": str(r["meeting_id"]),
                "role": r["role_code"] or "",
                "location": r["location_code"] or "",
            }
            for r in rows
        ],
    }


@router.get("/schedules/conducted")
def conducted_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """`status IN (1, 2)` — the same rows `units.completed` sums per meeting."""
    return _schedule_rows(meeting_ids, "status IN (1, 2)")


@router.get("/schedules/not-updated")
def not_updated_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """`status = 0` — the same rows `units.notConducted` sums per meeting."""
    return _schedule_rows(meeting_ids, "status = 0")


def _level_roster(level: str) -> list[tuple[str, str]]:
    """(id, name) pairs for every schedulable location at a level — the same
    universe `_not_scheduled_counts` measures a meeting against, just with the id kept alongside the
    name instead of collapsed to a count."""
    if level == "Unit":
        return [
            (str(r["unit_id"]), r["unit_code"] or "")
            for r in db.rows(
                """SELECT DISTINCT UT.id AS unit_id, UT.code AS unit_code
                     FROM booth B
                     JOIN unit UT ON B.unit_id = UT.id
                    WHERE B.publication_id = %s""",
                (config.UNIT_PUBLICATION_ID,),
            )
        ]
    if level == "Mandal":
        return [
            (str(r["location_id"]), r["location_name"] or "")
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        ]
    if level == "AC":
        return [(str(r["id"]), r["name"] or "") for r in db.rows("SELECT id, name FROM assembly")]
    if level == "PC":
        return [
            (str(r["id"]), r["parliament_name"] or "")
            for r in db.rows("SELECT id, parliament_name FROM parliament")
        ]
    return []


@router.get("/schedules/not-scheduled")
def not_scheduled_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """Roster locations with no `meeting_schedules` row at all for a meeting —
    the same figure `notScheduled` sums per meeting, drilled down to rows.

    Unlike every other slice in this file, this one has no `meeting_schedules`
    row to select — a location that was never scheduled has no row to find.
    It's built the other way round instead: the level's full roster, minus
    whichever of those ids this meeting's `entity_id`s do cover.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": []}
    marks = db.placeholders(ids)
    meeting_rows = db.rows(
        f"""SELECT m.id, l.level_name
              FROM meetings m
              LEFT JOIN meeting_levels l ON l.id = m.meeting_level_id
             WHERE m.id IN ({marks})""",
        tuple(ids),
    )
    scheduled = db.rows(
        f"SELECT meeting_id, entity_id FROM meeting_schedules WHERE meeting_id IN ({marks})",
        tuple(ids),
    )
    scheduled_by_meeting: dict[str, set[str]] = {}
    for r in scheduled:
        scheduled_by_meeting.setdefault(str(r["meeting_id"]), set()).add(str(r["entity_id"]))

    roster_cache: dict[str, list[tuple[str, str]]] = {}
    out_rows: list[dict[str, Any]] = []
    for mrow in meeting_rows:
        mid = str(mrow["id"])
        level = adapt.level_code(mrow["level_name"])
        if level not in roster_cache:
            roster_cache[level] = _level_roster(level)
        scheduled_ids = scheduled_by_meeting.get(mid, set())
        out_rows.extend(
            {"meetingId": mid, "location": name}
            for loc_id, name in roster_cache[level]
            if loc_id not in scheduled_ids
        )
    return {"total": len(out_rows), "rows": out_rows}


@router.get("/schedules/pc-completed")
def pc_completed_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """`is_conducted = 'Y'` — the same rows PC Status' Completed sums."""
    return _conducted_status_rows(meeting_ids, "is_conducted = 'Y'")


@router.get("/schedules/pc-not-completed")
def pc_not_completed_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """`is_conducted IS NULL OR 'N'` — PC Status' Not completed, the combined
    figure. Broader than `/pc-not-updated`, which is NULL alone."""
    return _conducted_status_rows(meeting_ids, "is_conducted IS NULL OR is_conducted = 'N'")


@router.get("/schedules/pc-not-updated")
def pc_not_updated_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """`is_conducted IS NULL` — never touched, kept apart from an explicit 'N'."""
    return _conducted_status_rows(meeting_ids, "is_conducted IS NULL")


def _remark_rows(meeting_ids: str) -> dict[str, Any]:
    """Row-level `meeting_remark` detail — the PC in-charge's own written note
    against a conducted-status row, alongside its `remarks_category` tag."""
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": []}
    marks = db.placeholders(ids)
    rows = db.rows(
        f"""SELECT mr.meeting_remark_id AS id, mr.meeting_id,
                   r.code AS role_code, COALESCE(u.code, mcs.location_id) AS location_code,
                   rc.category_name, mr.remarks
              FROM meeting_remark mr
              JOIN meeting_conducted_status mcs
                ON mcs.meeting_conducted_status_id = mr.meeting_conducted_status_id
              LEFT JOIN role r ON r.id = mcs.role_id
              LEFT JOIN unit u ON u.id = mcs.location_id
              LEFT JOIN remarks_category rc ON rc.remarks_category_id = mr.remarks_category_id
             WHERE mr.meeting_id IN ({marks})
               AND mr.remarks IS NOT NULL AND mr.remarks <> ''
             ORDER BY mr.meeting_id, mr.meeting_remark_id""",
        tuple(ids),
    )
    return {
        "total": len(rows),
        "rows": [
            {
                "id": r["id"],
                "meetingId": str(r["meeting_id"]),
                "role": r["role_code"] or "",
                "location": r["location_code"] or "",
                "category": r["category_name"] or "",
                "remarks": r["remarks"] or "",
            }
            for r in rows
        ],
    }


@router.get("/schedules/pc-remarks")
def pc_remarks_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
) -> dict[str, Any]:
    """Every non-empty PC remark — the same rows `pcRemarks` sums per meeting."""
    return _remark_rows(meeting_ids)


@router.get("/{meeting_id}/schedule-summary")
def schedule_summary(meeting_id: str) -> dict[str, Any]:
    """The App & PC summary panel's real row detail — one row per schedule.

    `meeting_schedules.entity_id` and `meeting_conducted_status.location_id`
    are the same id: confirmed 1:1 for meeting 22's 2,860 rows, so the join
    carries no fan-out. A location with no matching row there reads as not
    completed, the same as an explicit NULL — there is nothing to mark it
    complete with either way. `meeting_remark` carries at most one row per
    `meeting_conducted_status_id` — the save endpoint updates in place rather
    than versioning — so the join to it is 1:1 too.

    `entity_id` is a `unit.id` at Unit level, an `assembly.id` at AC level, a
    `parliament.id` at PC level, and a `mandal.id` or `town.id` at Mandal
    level — the id spaces overlap (assembly 207 and unit 207 both exist), so
    which table it resolves against has to follow the meeting's own level
    rather than "whichever join isn't null". Blindly joining `unit` for every
    row, as this used to, silently matched AC-, PC- and Mandal-level rows
    against an unrelated unit and showed its code as the location — a mandal
    committee is its own thing, not the `unit` rows nested inside it, so
    `mandal`/`town` (the two don't collide with each other) resolve it now.

    `entity_id`/`assembly_id` are `int`/`bigint`; every table above (plus
    `meeting_conducted_status.location_id`) keys off a `varchar` id. Left as
    `x.id = s.entity_id`, MySQL casts the varchar side to compare, which
    can't use that column's index — a full table scan per schedule row, for
    every one of these joins. The cast goes on the `meeting_schedules` side
    instead (same fix as the invitee/attendance join elsewhere in this file)
    so each target table's own primary key still does the work.

    The panel doesn't send a per-row `assembly` — the caller already knows
    the meeting's level (it's the tier the user picked to get here), so the
    UI reads that off its own `level` prop rather than a value repeated
    identically on every row. At AC level it does still send `parliament`
    (which parliament each assembly sits in) and at Mandal and Unit level
    `assembly` (which AC each mandal/town/division/unit sits in) — all
    genuinely vary row to row. The Mandal one comes straight off
    `s.assembly_id`, the schedule's own column, not off `entity_id` —
    `entity_id`'s id space collides with `assembly.id` too (mandal 141 and
    assembly 141 both exist), so it can't be trusted to resolve the AC on its
    own the way it resolves the unit. `unit` carries no `assembly_id` of its
    own that's safe to trust either, so the Unit one goes through `booth` —
    the same (assembly, unit) pairing `/api/units` already counts from,
    scoped to the live roll (`config.UNIT_PUBLICATION_ID`). That map is
    fetched once as its own query and applied in Python rather than joined
    per row: `booth` has no index MySQL can use against a `DISTINCT`
    subquery, so joining it turned into a nested scan of ~46k booth rows for
    every one of a meeting's schedule rows and never finished.

    At Mandal level, `location` prefers the enrolled committee roster behind
    `/api/committees/mandal-town-division` (the "Mandal / Town / Division
    Level · Total" card) over `mandal`/`town`: those two mytdp tables only
    cover 84% of this tier's schedule rows, while the roster — filtered the
    same way the Total card is (`tdp_committee_enrollment_id = 4`, levels
    5/7/9, `tdp_basic_committee_id = 1`) — covers 99.75% and, within that
    filtered set, never has two different committees sharing one location id
    (checked directly; the *unfiltered* `tehsil`/`local_election_body`
    tables do collide, which is why this endpoint doesn't query them
    unfiltered). `mandal`/`town` remain the fallback for the handful of rows
    the roster doesn't cover.
    """
    rows = db.rows(
        """SELECT s.id, s.entity_id, ml.level_name, s.location_text,
                  u.id AS unit_id, u.code AS unit_code, ae.name AS entity_assembly_name,
                  pe.parliament_name AS entity_parliament_name,
                  pac.parliament_name AS ac_parliament_name,
                  ma.name AS mandal_assembly_name,
                  md.name AS mandal_name, tn.town_name,
                  s.status, s.updated_at, c.is_conducted,
                  c.meeting_conducted_status_id, mr.remarks_category_id, mr.remarks
             FROM meeting_schedules s
             JOIN meetings mt ON mt.id = s.meeting_id
             LEFT JOIN meeting_levels ml ON ml.id = mt.meeting_level_id
             LEFT JOIN unit u ON u.id = CAST(s.entity_id AS CHAR)
             LEFT JOIN assembly ae ON ae.id = CAST(s.entity_id AS CHAR)
             LEFT JOIN parliament pe ON pe.id = CAST(s.entity_id AS CHAR)
             LEFT JOIN parliament pac ON pac.id = ae.parliament_id
             LEFT JOIN assembly ma ON ma.id = CAST(s.assembly_id AS CHAR)
             LEFT JOIN mandal md ON md.id = CAST(s.entity_id AS CHAR)
             LEFT JOIN town tn ON tn.id = CAST(s.entity_id AS CHAR)
             LEFT JOIN meeting_conducted_status c
                    ON c.meeting_id = s.meeting_id AND c.location_id = CAST(s.entity_id AS CHAR)
             LEFT JOIN meeting_remark mr
                    ON mr.meeting_conducted_status_id = c.meeting_conducted_status_id
            WHERE s.meeting_id = %s
            ORDER BY s.id""",
        (meeting_id,),
    )

    unit_assembly: dict[str, str] = {}
    if any(adapt.level_code(r["level_name"]) == "Unit" for r in rows):
        unit_assembly = {
            str(r["unit_id"]): r["assembly_code"] or ""
            for r in db.rows(
                """SELECT DISTINCT UT.id AS unit_id, AC.code AS assembly_code
                     FROM booth B
                     JOIN assembly AC ON B.assembly_id = AC.id AND B.publication_id = %s
                     JOIN unit UT ON B.unit_id = UT.id""",
                (config.UNIT_PUBLICATION_ID,),
            )
        }

    mandal_locations: dict[str, str] = {}
    if any(adapt.level_code(r["level_name"]) == "Mandal" for r in rows):
        mandal_locations = {
            str(r["location_id"]): r["location_name"] or ""
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        }

    def row_out(r: dict[str, Any]) -> dict[str, Any]:
        level = adapt.level_code(r["level_name"])
        if level == "PC":
            location = r["entity_parliament_name"] or r["location_text"] or ""
        elif level == "AC":
            location = r["entity_assembly_name"] or r["location_text"] or ""
        elif level == "Mandal":
            location = (
                mandal_locations.get(str(r["entity_id"]))
                or r["mandal_name"] or r["town_name"] or r["location_text"] or ""
            )
        else:
            location = r["unit_code"] or r["location_text"] or ""
        if level == "Mandal":
            assembly = r["mandal_assembly_name"] or ""
        elif level == "Unit":
            assembly = unit_assembly.get(str(r["unit_id"]), "")
        else:
            assembly = ""
        return {
            "id": r["id"],
            "parliament": r["ac_parliament_name"] or "" if level == "AC" else "",
            "assembly": assembly,
            "location": location,
            "appConducted": r["status"] in (1, 2),
            "pcConducted": r["is_conducted"] == "Y",
            "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
            "conductedStatusId": r["meeting_conducted_status_id"],
            "categoryId": r["remarks_category_id"],
            "remarks": r["remarks"] or "",
        }

    return {"total": len(rows), "rows": [row_out(r) for r in rows]}


class ConductedRemarkIn(BaseModel):
    categoryId: int | None = None
    remarks: str = Field(default="", max_length=config.MAX_REMARKS_CHARS)


@router.put("/conducted-status/{status_id}/remark")
def save_conducted_remark(
    status_id: str, body: ConductedRemarkIn = Body(...)
) -> dict[str, Any]:
    """Record (or clear) the PC in-charge's remark against one conducted-status row.

    `meeting_remark` is exclusive to this app and carries no soft-delete column
    the way `feedback_comment` does, so a save just updates the one row for this
    status id in place — the same row a fresh `schedule-summary` fetch reads.
    """
    status = db.one(
        "SELECT meeting_id FROM meeting_conducted_status WHERE meeting_conducted_status_id = %s",
        (status_id,),
    )
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown conducted-status row")

    remarks = body.remarks.strip()
    existing = db.one(
        "SELECT meeting_remark_id FROM meeting_remark WHERE meeting_conducted_status_id = %s",
        (status_id,),
    )
    if existing:
        db.execute(
            """UPDATE meeting_remark
                  SET remarks_category_id = %s, remarks = %s, inserted_time = NOW()
                WHERE meeting_remark_id = %s""",
            (body.categoryId, remarks, existing["meeting_remark_id"]),
        )
    else:
        db.execute(
            """INSERT INTO meeting_remark
                   (meeting_id, meeting_conducted_status_id, remarks_category_id, remarks, inserted_time)
               VALUES (%s, %s, %s, %s, NOW())""",
            (status["meeting_id"], status_id, body.categoryId, remarks),
        )
    return {"conductedStatusId": status_id, "categoryId": body.categoryId, "remarks": remarks}


@router.get("/{meeting_id}")
def get_meeting(meeting_id: str) -> dict[str, Any]:
    row = db.one(_MEETING_COLS + " WHERE m.id = %s", (meeting_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown meeting")
    agg_all, not_scheduled_all = db.parallel(
        lambda: _aggregates([str(row["id"])]),
        lambda: _not_scheduled_counts([row]),
    )
    agg = agg_all.get(str(row["id"]), {})
    not_scheduled = not_scheduled_all.get(str(row["id"]), 0)
    return adapt.meeting(row, agg, not_scheduled)


_MEMBER_COLS = f"""
SELECT i.membership_id, i.member_name, i.mobile_no, i.level_name,
       i.committee_name, i.role_name,
       i.constituency_name AS ac, p.parliament_name AS pc,
       (a.mid IS NOT NULL) AS present,
       f.remarks, f.inserted_user_id AS captured_by
  FROM meeting_invitee i
  LEFT JOIN assembly asm ON asm.id = i.constituency_id
  LEFT JOIN parliament p ON p.id = asm.parliament_id
  LEFT JOIN {_ATTENDED} a
         ON a.meeting_id = CAST(i.meeting_id AS UNSIGNED)
        AND a.mid = i.membership_id
  LEFT JOIN feedback_comment f
         ON f.program_id = i.meeting_id
        AND f.membership_id = i.membership_id
        AND f.feeback_program_type_id = {config.MEETING_TYPE_ID}
        AND COALESCE(f.is_deleted, 'N') <> 'Y'
"""


@router.get("/{meeting_id}/members")
def list_members(
    meeting_id: str,
    ac: str | None = Query(None, description="Assembly name, exact match"),
    limit: int = Query(config.MAX_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """A page of this meeting's invitees, absentees first."""
    where = "WHERE i.meeting_id = %s" + (" AND i.constituency_name = %s" if ac else "")
    args: tuple = (meeting_id, ac) if ac else (meeting_id,)

    total = db.scalar(f"SELECT COUNT(*) FROM meeting_invitee i {where}", args)
    rows = db.rows(
        f"{_MEMBER_COLS} {where} ORDER BY present, i.member_name LIMIT %s OFFSET %s",
        (*args, limit, offset),
    )
    acs = [
        r["ac"]
        for r in db.rows(
            """SELECT DISTINCT constituency_name AS ac FROM meeting_invitee
                WHERE meeting_id = %s AND constituency_name <> ''
                ORDER BY constituency_name""",
            (meeting_id,),
        )
    ]
    return {"total": adapt.num(total), "acs": acs, "rows": [adapt.member(r) for r in rows]}


_ROLLUP = f"""
SELECT {{cols}},
       COUNT(*) AS invited,
       SUM(a.mid IS NOT NULL) AS attended,
       SUM(a.mid IS NULL AND f.remarks IS NOT NULL AND f.remarks <> '') AS captured
  FROM meeting_invitee i
  LEFT JOIN assembly asm ON asm.id = i.constituency_id
  LEFT JOIN parliament p ON p.id = asm.parliament_id
  LEFT JOIN {_ATTENDED} a
         ON a.meeting_id = CAST(i.meeting_id AS UNSIGNED)
        AND a.mid = i.membership_id
  LEFT JOIN feedback_comment f
         ON f.program_id = i.meeting_id
        AND f.membership_id = i.membership_id
        AND f.feeback_program_type_id = {config.MEETING_TYPE_ID}
        AND COALESCE(f.is_deleted, 'N') <> 'Y'
 WHERE i.meeting_id = %s
 GROUP BY {{group}}
"""


@router.get("/{meeting_id}/rollup")
def meeting_rollup(meeting_id: str) -> dict[str, Any]:
    """This meeting's posture across every PC and AC, over the whole list.

    The member table serves a page at a time; these figures are counted over all
    of it, so the headline never describes only the rows on screen.
    """
    if db.scalar("SELECT 1 FROM meetings WHERE id = %s", (meeting_id,)) is None:
        raise HTTPException(status_code=404, detail="Unknown meeting")

    totals = db.one(_ROLLUP.format(cols="'' AS all_rows", group="1"), (meeting_id,))
    by_pc = db.rows(
        _ROLLUP.format(cols="p.parliament_name AS pc", group="p.parliament_name"),
        (meeting_id,),
    )
    by_ac = db.rows(
        _ROLLUP.format(
            cols="p.parliament_name AS pc, i.constituency_name AS ac",
            group="p.parliament_name, i.constituency_name",
        ),
        (meeting_id,),
    )

    # worst first: the point of the view is finding what has not been done yet
    worst = lambda d: (d["completion"], -d["pending"])  # noqa: E731
    empty = {"invited": 0, "attended": 0, "captured": 0}
    return {
        "totals": adapt.posture(totals or empty),
        "byPc": sorted(
            (adapt.posture(r, pc=r["pc"] or "") for r in by_pc), key=worst
        ),
        "byAc": sorted(
            (adapt.posture(r, pc=r["pc"] or "", ac=r["ac"] or "") for r in by_ac),
            key=worst,
        ),
    }


@router.put("/{meeting_id}/members/{mid}/remarks")
def save_remarks(
    meeting_id: str, mid: str, body: RemarksIn = Body(...)
) -> dict[str, Any]:
    """Record (or clear) one absentee's feedback remark.

    Returns the member and the meeting together: the meeting's completion moves
    with the save, and the UI would otherwise have to refetch. An empty remark
    clears the capture — as a soft delete, because `feedback_comment` is shared
    with the rest of the party estate and rows there are not ours to destroy.
    """
    invitee = db.one(
        f"""{_MEMBER_COLS} WHERE i.meeting_id = %s AND i.membership_id = %s LIMIT 1""",
        (meeting_id, mid),
    )
    if invitee is None:
        raise HTTPException(status_code=404, detail="Unknown member")
    if invitee["present"]:
        raise HTTPException(
            status_code=400, detail="Feedback is captured from absent members only"
        )

    remarks = body.remarks.strip()
    existing = db.one(
        """SELECT feedback_comment_id FROM feedback_comment
            WHERE feeback_program_type_id = %s AND program_id = %s AND membership_id = %s
            ORDER BY feedback_comment_id DESC LIMIT 1""",
        (config.MEETING_TYPE_ID, meeting_id, mid),
    )

    if existing:
        db.execute(
            """UPDATE feedback_comment
                  SET remarks = %s,
                      feedback_taken = %s,
                      is_deleted = %s,
                      updated_time = NOW()
                WHERE feedback_comment_id = %s""",
            (remarks, "Y" if remarks else "N", "N" if remarks else "Y",
             existing["feedback_comment_id"]),
        )
    elif remarks:
        meeting_row = db.one("SELECT title FROM meetings WHERE id = %s", (meeting_id,))
        db.execute(
            """INSERT INTO feedback_comment
                   (feeback_program_type_id, program_id, program_name, membership_id,
                    member_name, mobile_no, location_name, is_attended, feedback_taken,
                    remarks, is_deleted, inserted_time)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'N', 'Y', %s, 'N', NOW())""",
            (
                config.MEETING_TYPE_ID, meeting_id,
                (meeting_row or {}).get("title") or "",
                mid,
                invitee["member_name"], invitee["mobile_no"], invitee["ac"],
                remarks,
            ),
        )

    saved = db.one(
        f"""{_MEMBER_COLS} WHERE i.meeting_id = %s AND i.membership_id = %s LIMIT 1""",
        (meeting_id, mid),
    )
    return {"member": adapt.member(saved or invitee), "meeting": get_meeting(meeting_id)}
