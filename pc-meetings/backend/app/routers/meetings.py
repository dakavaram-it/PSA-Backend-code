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


# The PC-side twin of `_ROSTER_JOINS`, joined against
# `meeting_conducted_status.location_id` instead of `meeting_schedules.entity_id`.
# `location_id` is already a varchar (the same column `CAST(s.entity_id AS CHAR)`
# gets compared to elsewhere in this file), so no cast is needed on this side.
_PC_ROSTER_JOINS = {
    "Unit": ("""JOIN unit u ON u.id = mcs.location_id
                JOIN booth b ON b.unit_id = u.id AND b.publication_id = %s""", True),
    "AC": ("JOIN assembly a ON a.id = mcs.location_id", False),
    "PC": ("JOIN parliament p ON p.id = mcs.location_id", False),
}


def _pc_matched_counts(by_level: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Per level and meeting id: how many distinct roster locations that
    meeting's own `meeting_conducted_status` rows land on — the PC-side twin
    of `_matched_counts`."""
    parts, args = [], []
    for level, ids in by_level.items():
        join, needs_publication = _PC_ROSTER_JOINS[level]
        parts.append(f"""
            SELECT '{level}' AS level, mcs.meeting_id AS id, COUNT(DISTINCT mcs.location_id) AS matched
              FROM meeting_conducted_status mcs
              {join}
             WHERE mcs.meeting_id IN ({db.placeholders(ids)})
             GROUP BY mcs.meeting_id""")
        if needs_publication:
            args.append(config.UNIT_PUBLICATION_ID)
        args.extend(ids)

    out: dict[str, dict[str, int]] = {level: {} for level in by_level}
    for r in db.rows(" UNION ALL ".join(parts), tuple(args)):
        out[r["level"]][str(r["id"])] = r["matched"]
    return out


def _pc_not_updated_counts(meeting_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Per meeting: how many of its level's roster locations have no
    `meeting_conducted_status` row at all — the PC-side twin of
    `_not_scheduled_counts`, so "PC Not Updated" means the same thing PC-side
    that "Not Updated"/"never scheduled" already means App-side, rather than
    the explicit-`'N'` remainder it used to mean.

    Same reasoning throughout: only the roster ids a meeting's own
    `location_id`s actually cover get subtracted, never a flat roster-size
    difference, and Mandal is diffed in Python against `dakavara_pa`'s
    roster since it shares no id space with `mytdp` to join on.
    """
    by_level: dict[str, list[str]] = {}
    for r in meeting_rows:
        by_level.setdefault(adapt.level_code(r["level_name"]), []).append(str(r["id"]))

    out: dict[str, int] = {}

    joined = {lvl: ids for lvl, ids in by_level.items() if lvl in _PC_ROSTER_JOINS}
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
        matched = _pc_matched_counts(joined)
        for level, ids in joined.items():
            for mid in ids:
                out[mid] = max(sizes.get(level, 0) - matched[level].get(mid, 0), 0)

    mandal_ids = by_level.get("Mandal", [])
    if mandal_ids:
        marks = db.placeholders(mandal_ids)
        roster_ids = {str(r["location_id"]) for r in db.rows(_COMMITTEE_LOCATIONS) if r["location_id"] is not None}
        covered_by_meeting: dict[str, set[str]] = {}
        for r in db.rows(
            f"SELECT meeting_id, location_id FROM meeting_conducted_status WHERE meeting_id IN ({marks})",
            tuple(mandal_ids),
        ):
            covered_by_meeting.setdefault(str(r["meeting_id"]), set()).add(str(r["location_id"]))
        for mid in mandal_ids:
            matched_mandal = len(roster_ids & covered_by_meeting.get(mid, set()))
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
    # None of the three groups needs another's answer and each is seconds of
    # database work, so they overlap rather than queue.
    agg, not_scheduled, pc_not_updated = db.parallel(
        lambda: _aggregates([str(r["id"]) for r in rows]),
        lambda: _not_scheduled_counts(rows),
        lambda: _pc_not_updated_counts(rows),
    )
    return [
        adapt.meeting(
            r, agg.get(str(r["id"]), {}), not_scheduled.get(str(r["id"]), 0),
            pc_not_updated.get(str(r["id"]), 0),
        )
        for r in rows
    ]


def _ids(meeting_ids: str) -> list[str]:
    return [i for i in meeting_ids.split(",") if i]


def _unit_lookup(unit_ids: list[str] | None = None) -> dict[str, dict[str, str]]:
    """Per unit id: code plus the assembly/parliament its live booth roll sits
    in — the same (assembly, unit) pairing `/api/units` counts from, scoped to
    the live roll (`config.UNIT_PUBLICATION_ID`).

    Driven off `unit` (PK IN list) rather than scanning `booth` first: Unit
    App & PC Summary can ask for thousands of ids, and a booth-led DISTINCT
    was the multi-second cost of opening that panel. Results are chunked so a
    single giant ``IN`` list does not blow the packet either.
    """
    if unit_ids is not None and not unit_ids:
        return {}

    def _chunk(ids: list[str]) -> dict[str, dict[str, str]]:
        def _one(part: list[str]) -> dict[str, dict[str, str]]:
            marks = db.placeholders(part)
            return {
                str(r["unit_id"]): {
                    "code": r["unit_code"] or "",
                    "assembly": r["assembly_code"] or "",
                    "parliament": r["parliament_name"] or "",
                }
                for r in db.rows(
                    f"""SELECT UT.id AS unit_id, UT.code AS unit_code,
                               AC.code AS assembly_code, PC.parliament_name AS parliament_name
                          FROM unit UT
                          LEFT JOIN (
                                SELECT unit_id, MIN(assembly_id) AS assembly_id
                                  FROM booth
                                 WHERE publication_id = %s AND unit_id IN ({marks})
                                 GROUP BY unit_id
                          ) B ON B.unit_id = UT.id
                          LEFT JOIN assembly AC ON AC.id = B.assembly_id
                          LEFT JOIN parliament PC ON PC.id = AC.parliament_id
                         WHERE UT.id IN ({marks})""",
                    (config.UNIT_PUBLICATION_ID, *part, *part),
                )
            }

        parts = [ids[i : i + 800] for i in range(0, len(ids), 800)]
        if len(parts) == 1:
            return _one(parts[0])
        out: dict[str, dict[str, str]] = {}
        # Parallel chunks — Unit drills often ask for 2–8k ids; sequential
        # booth subqueries were the remaining multi-second cost.
        for chunk in db.parallel(*[ (lambda p=part: _one(p)) for part in parts ]):
            out.update(chunk)
        return out

    if unit_ids is not None:
        return _chunk(list(dict.fromkeys(unit_ids)))

    # Full roll — used by schedule drill-downs that do not know the id set yet.
    return {
        str(r["unit_id"]): {
            "code": "",
            "assembly": r["assembly_code"] or "",
            "parliament": r["parliament_name"] or "",
        }
        for r in db.rows(
            """SELECT DISTINCT UT.id AS unit_id, AC.code AS assembly_code, PC.parliament_name AS parliament_name
                 FROM booth B
                 JOIN assembly AC ON B.assembly_id = AC.id AND B.publication_id = %s
                 JOIN unit UT ON B.unit_id = UT.id
                 LEFT JOIN parliament PC ON PC.id = AC.parliament_id""",
            (config.UNIT_PUBLICATION_ID,),
        )
    }


def _schedule_rows(
    meeting_ids: str,
    condition: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Row-level `meeting_schedules` detail behind an App-side figure.

    Lean select (no per-row CAST joins onto unit/assembly/parliament) plus a
    level-scoped name batch — the CAST fan-out plus a full `_unit_lookup()`
    was what made App Conducted / Not Conducted lag on Unit meetings.

    `limit`/`offset` page the schedule rows and only resolve place names for
    that page, so the first paint of a Unit drill can return in ~1s.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}
    marks = db.placeholders(ids)
    total = db.rows(
        f"""SELECT COUNT(*) AS c FROM meeting_schedules s
             WHERE s.meeting_id IN ({marks}) AND {condition}""",
        tuple(ids),
    )[0]["c"]
    if not total:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}

    args: list[Any] = list(ids)
    lim_sql = ""
    if limit is not None:
        lim_sql = " LIMIT %s OFFSET %s"
        args.extend([max(0, int(limit)), max(0, int(offset))])

    rows = db.rows(
        f"""SELECT s.id, s.meeting_id, s.entity_id, s.location_text, s.meeting_time,
                   s.assembly_id, ml.level_name, r.code AS role_code
              FROM meeting_schedules s
              JOIN meetings mt ON mt.id = s.meeting_id
              LEFT JOIN meeting_levels ml ON ml.id = mt.meeting_level_id
              LEFT JOIN role r ON r.id = s.role_id
             WHERE s.meeting_id IN ({marks}) AND {condition}
             ORDER BY s.meeting_id, s.id{lim_sql}""",
        tuple(args),
    )
    if not rows:
        return {"total": total, "rows": [], "limit": limit, "offset": offset}

    levels = {adapt.level_code(r["level_name"]) for r in rows}
    unit_lookup: dict[str, dict[str, str]] = {}
    assembly_names: dict[str, str] = {}
    ac_parliament: dict[str, str] = {}
    parliament_names: dict[str, str] = {}
    mandal_locations: dict[str, str] = {}
    mandal_assembly: dict[str, str] = {}
    mandal_parliament: dict[str, str] = {}

    if "Unit" in levels:
        entity_ids = [
            str(r["entity_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "Unit" and r["entity_id"] is not None
        ]
        unit_lookup = _unit_lookup(entity_ids)
    if "AC" in levels:
        ac_ids = [
            str(r["entity_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "AC" and r["entity_id"] is not None
        ]
        if ac_ids:
            for r in db.rows(
                f"""SELECT a.id, a.name, p.parliament_name
                      FROM assembly a
                      LEFT JOIN parliament p ON p.id = a.parliament_id
                     WHERE a.id IN ({db.placeholders(ac_ids)})""",
                tuple(dict.fromkeys(ac_ids)),
            ):
                assembly_names[str(r["id"])] = r["name"] or ""
                ac_parliament[str(r["id"])] = r["parliament_name"] or ""
    if "PC" in levels:
        pc_ids = [
            str(r["entity_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "PC" and r["entity_id"] is not None
        ]
        if pc_ids:
            for r in db.rows(
                f"SELECT id, parliament_name FROM parliament WHERE id IN ({db.placeholders(pc_ids)})",
                tuple(dict.fromkeys(pc_ids)),
            ):
                parliament_names[str(r["id"])] = r["parliament_name"] or ""
    if "Mandal" in levels:
        mandal_locations = {
            str(r["location_id"]): r["location_name"] or ""
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        }
        mandal_ids = [
            str(r["entity_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "Mandal" and r["entity_id"] is not None
        ]
        if mandal_ids:
            uniq = list(dict.fromkeys(mandal_ids))
            for r in db.rows(
                f"SELECT id, name FROM mandal WHERE id IN ({db.placeholders(uniq)})",
                tuple(uniq),
            ):
                mandal_locations.setdefault(str(r["id"]), r["name"] or "")
            for r in db.rows(
                f"SELECT id, town_name FROM town WHERE id IN ({db.placeholders(uniq)})",
                tuple(uniq),
            ):
                mandal_locations.setdefault(str(r["id"]), r["town_name"] or "")
        ac_ids = list({
            str(r["assembly_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "Mandal" and r["assembly_id"] is not None
        })
        if ac_ids:
            for r in db.rows(
                f"""SELECT a.id, a.name, p.parliament_name
                      FROM assembly a
                      LEFT JOIN parliament p ON p.id = a.parliament_id
                     WHERE a.id IN ({db.placeholders(ac_ids)})""",
                tuple(ac_ids),
            ):
                mandal_assembly[str(r["id"])] = r["name"] or ""
                mandal_parliament[str(r["id"])] = r["parliament_name"] or ""

    def _fields(r: dict[str, Any]) -> dict[str, str]:
        level = adapt.level_code(r["level_name"])
        loc = str(r["entity_id"]) if r["entity_id"] is not None else ""
        if level == "PC":
            return {
                "location": parliament_names.get(loc) or r["location_text"] or "",
                "assembly": "", "parliament": "",
            }
        if level == "AC":
            return {
                "location": assembly_names.get(loc) or r["location_text"] or "",
                "assembly": "", "parliament": ac_parliament.get(loc, ""),
            }
        if level == "Mandal":
            ac = str(r["assembly_id"]) if r["assembly_id"] is not None else ""
            return {
                "location": mandal_locations.get(loc) or r["location_text"] or "",
                "assembly": mandal_assembly.get(ac, ""),
                "parliament": mandal_parliament.get(ac, ""),
            }
        info = unit_lookup.get(loc, {})
        return {
            "location": info.get("code") or r["location_text"] or "",
            "assembly": info.get("assembly", ""),
            "parliament": info.get("parliament", ""),
        }

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": [
            {
                "id": r["id"],
                "meetingId": str(r["meeting_id"]),
                "time": r["meeting_time"] or "",
                "role": r["role_code"] or "",
                **_fields(r),
            }
            for r in rows
        ],
    }


def _conducted_status_rows(
    meeting_ids: str,
    condition: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Row-level `meeting_conducted_status` detail behind a PC-side figure.

    Lean select + level-scoped name batches (same shape as `_schedule_rows`).
    `limit`/`offset` page MCS rows and only resolve place names for that page.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}
    marks = db.placeholders(ids)
    total = db.rows(
        f"""SELECT COUNT(*) AS c FROM meeting_conducted_status mcs
             WHERE mcs.meeting_id IN ({marks}) AND {condition}""",
        tuple(ids),
    )[0]["c"]
    if not total:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}

    args: list[Any] = list(ids)
    lim_sql = ""
    if limit is not None:
        lim_sql = " LIMIT %s OFFSET %s"
        args.extend([max(0, int(limit)), max(0, int(offset))])

    rows = db.rows(
        f"""SELECT mcs.meeting_conducted_status_id AS id, mcs.meeting_id, mcs.location_id,
                   ml.level_name, r.code AS role_code
              FROM meeting_conducted_status mcs
              JOIN meetings mt ON mt.id = mcs.meeting_id
              LEFT JOIN meeting_levels ml ON ml.id = mt.meeting_level_id
              LEFT JOIN role r ON r.id = mcs.role_id
             WHERE mcs.meeting_id IN ({marks}) AND {condition}
             ORDER BY mcs.meeting_id, mcs.meeting_conducted_status_id{lim_sql}""",
        tuple(args),
    )
    if not rows:
        return {"total": total, "rows": [], "limit": limit, "offset": offset}

    levels = {adapt.level_code(r["level_name"]) for r in rows}
    unit_lookup: dict[str, dict[str, str]] = {}
    assembly_names: dict[str, str] = {}
    ac_parliament: dict[str, str] = {}
    parliament_names: dict[str, str] = {}
    mandal_locations: dict[str, str] = {}

    if "Unit" in levels:
        unit_lookup = _unit_lookup([
            str(r["location_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "Unit" and r["location_id"] is not None
        ])
    if "AC" in levels:
        ac_ids = [
            str(r["location_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "AC" and r["location_id"] is not None
        ]
        if ac_ids:
            for r in db.rows(
                f"""SELECT a.id, a.name, p.parliament_name
                      FROM assembly a
                      LEFT JOIN parliament p ON p.id = a.parliament_id
                     WHERE a.id IN ({db.placeholders(ac_ids)})""",
                tuple(dict.fromkeys(ac_ids)),
            ):
                assembly_names[str(r["id"])] = r["name"] or ""
                ac_parliament[str(r["id"])] = r["parliament_name"] or ""
    if "PC" in levels:
        pc_ids = [
            str(r["location_id"]) for r in rows
            if adapt.level_code(r["level_name"]) == "PC" and r["location_id"] is not None
        ]
        if pc_ids:
            for r in db.rows(
                f"SELECT id, parliament_name FROM parliament WHERE id IN ({db.placeholders(pc_ids)})",
                tuple(dict.fromkeys(pc_ids)),
            ):
                parliament_names[str(r["id"])] = r["parliament_name"] or ""
    if "Mandal" in levels:
        mandal_locations = {
            str(r["location_id"]): r["location_name"] or ""
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        }

    def _fields(r: dict[str, Any]) -> dict[str, str]:
        level = adapt.level_code(r["level_name"])
        loc = str(r["location_id"]) if r["location_id"] is not None else ""
        if level == "PC":
            return {
                "location": parliament_names.get(loc) or loc,
                "assembly": "", "parliament": "",
            }
        if level == "AC":
            return {
                "location": assembly_names.get(loc) or loc,
                "assembly": "", "parliament": ac_parliament.get(loc, ""),
            }
        if level == "Mandal":
            return {
                "location": mandal_locations.get(loc) or loc,
                "assembly": "", "parliament": "",
            }
        info = unit_lookup.get(loc, {})
        return {
            "location": info.get("code") or loc,
            "assembly": info.get("assembly", ""),
            "parliament": info.get("parliament", ""),
        }

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": [
            {
                "id": r["id"],
                "meetingId": str(r["meeting_id"]),
                "role": r["role_code"] or "",
                **_fields(r),
            }
            for r in rows
        ],
    }


@router.get("/schedules/conducted")
def conducted_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`status IN (1, 2)` — the same rows `units.completed` sums per meeting."""
    return _schedule_rows(meeting_ids, "s.status IN (1, 2)", limit=limit, offset=offset)


@router.get("/schedules/not-updated")
def not_updated_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`status = 0` — the same rows `units.notConducted` sums per meeting."""
    return _schedule_rows(meeting_ids, "s.status = 0", limit=limit, offset=offset)


def _level_roster(level: str) -> list[tuple[str, str, str, str]]:
    """(id, name, assembly, parliament) for every schedulable location at a
    level — the same universe `_not_scheduled_counts` measures a meeting
    against, just with the id kept alongside the name instead of collapsed
    to a count.

    Assembly/parliament are only filled where they can be resolved without a
    `meeting_schedules` row to key off (there is none here — these locations
    were never scheduled): Unit through the same booth-roll lookup
    `_schedule_rows` uses, AC through its own `parliament_id`. Mandal has no
    such row-free path — `/schedule-summary` and `_schedule_rows` resolve a
    mandal's assembly from the *schedule's* `assembly_id`, which a
    never-scheduled location has none of, and the enrolled committee
    roster's own `constituency_name` (from `dakavara_pa`) is a different
    schema's idea of "assembly" that hasn't been confirmed to line up with
    it — so it's left blank rather than shown and risk being wrong.
    """
    if level == "Unit":
        unit_rows = db.rows(
            """SELECT DISTINCT UT.id AS unit_id, UT.code AS unit_code
                 FROM booth B
                 JOIN unit UT ON B.unit_id = UT.id
                WHERE B.publication_id = %s""",
            (config.UNIT_PUBLICATION_ID,),
        )
        lookup = _unit_lookup([str(r["unit_id"]) for r in unit_rows])
        return [
            (
                str(r["unit_id"]),
                lookup.get(str(r["unit_id"]), {}).get("code") or r["unit_code"] or "",
                lookup.get(str(r["unit_id"]), {}).get("assembly", ""),
                lookup.get(str(r["unit_id"]), {}).get("parliament", ""),
            )
            for r in unit_rows
        ]
    if level == "Mandal":
        return [
            (str(r["location_id"]), r["location_name"] or "", "", "")
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        ]
    if level == "AC":
        return [
            (str(r["id"]), r["name"] or "", "", r["parliament_name"] or "")
            for r in db.rows(
                """SELECT a.id, a.name, p.parliament_name
                     FROM assembly a
                     LEFT JOIN parliament p ON p.id = a.parliament_id"""
            )
        ]
    if level == "PC":
        return [
            (str(r["id"]), r["parliament_name"] or "", "", "")
            for r in db.rows("SELECT id, parliament_name FROM parliament")
        ]
    return []


@router.get("/schedules/not-scheduled")
def not_scheduled_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Roster locations with no `meeting_schedules` row at all for a meeting —
    the same figure `notScheduled` sums per meeting, drilled down to rows.

    Unlike every other slice in this file, this one has no `meeting_schedules`
    row to select — a location that was never scheduled has no row to find.
    It's built the other way round instead: the level's full roster, minus
    whichever of those ids this meeting's `entity_id`s do cover.

    Unit: assembly/parliament are resolved only for the *page* of gap ids —
    looking up the entire live roll was the multi-second cost of App Not Updated.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}
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

    roster_cache: dict[str, list[tuple[str, str, str, str]]] = {}
    unit_roll: list[tuple[str, str]] | None = None
    all_rows: list[dict[str, Any]] = []
    for mrow in meeting_rows:
        mid = str(mrow["id"])
        level = adapt.level_code(mrow["level_name"])
        scheduled_ids = scheduled_by_meeting.get(mid, set())
        if level == "Unit":
            if unit_roll is None:
                unit_roll = [
                    (str(r["unit_id"]), r["unit_code"] or "")
                    for r in db.rows(
                        """SELECT DISTINCT UT.id AS unit_id, UT.code AS unit_code
                             FROM booth B
                             JOIN unit UT ON B.unit_id = UT.id
                            WHERE B.publication_id = %s""",
                        (config.UNIT_PUBLICATION_ID,),
                    )
                ]
            all_rows.extend(
                {"meetingId": mid, "location": code, "assembly": "", "parliament": "", "_uid": uid}
                for uid, code in unit_roll
                if uid not in scheduled_ids
            )
            continue
        if level not in roster_cache:
            roster_cache[level] = _level_roster(level)
        all_rows.extend(
            {"meetingId": mid, "location": name, "assembly": assembly, "parliament": parliament}
            for loc_id, name, assembly, parliament in roster_cache[level]
            if loc_id not in scheduled_ids
        )

    total = len(all_rows)
    page = all_rows[offset: offset + limit] if limit is not None else all_rows[offset:]
    unit_page = [r for r in page if "_uid" in r]
    if unit_page:
        lookup = _unit_lookup([r["_uid"] for r in unit_page])
        for r in unit_page:
            info = lookup.get(r["_uid"], {})
            r["location"] = info.get("code") or r["location"]
            r["assembly"] = info.get("assembly", "")
            r["parliament"] = info.get("parliament", "")
            del r["_uid"]
    for r in page:
        r.pop("_uid", None)
    return {"total": total, "limit": limit, "offset": offset, "rows": page}


@router.get("/schedules/pc-never-updated")
def pc_never_updated_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Roster locations with no `meeting_conducted_status` row at all for a
    meeting — the same figure PC Status' `notUpdated` sums per meeting,
    drilled down to rows. The PC-side twin of `/schedules/not-scheduled`.

    Unit gap rows only run `_unit_lookup` on the requested page of missing ids.
    """
    ids = _ids(meeting_ids)
    if not ids:
        return {"total": 0, "rows": [], "limit": limit, "offset": offset}
    marks = db.placeholders(ids)
    meeting_rows = db.rows(
        f"""SELECT m.id, l.level_name
              FROM meetings m
              LEFT JOIN meeting_levels l ON l.id = m.meeting_level_id
             WHERE m.id IN ({marks})""",
        tuple(ids),
    )
    covered = db.rows(
        f"SELECT meeting_id, location_id FROM meeting_conducted_status WHERE meeting_id IN ({marks})",
        tuple(ids),
    )
    covered_by_meeting: dict[str, set[str]] = {}
    for r in covered:
        covered_by_meeting.setdefault(str(r["meeting_id"]), set()).add(str(r["location_id"]))

    roster_cache: dict[str, list[tuple[str, str, str, str]]] = {}
    unit_roll: list[tuple[str, str]] | None = None
    all_rows: list[dict[str, Any]] = []
    for mrow in meeting_rows:
        mid = str(mrow["id"])
        level = adapt.level_code(mrow["level_name"])
        covered_ids = covered_by_meeting.get(mid, set())
        if level == "Unit":
            if unit_roll is None:
                unit_roll = [
                    (str(r["unit_id"]), r["unit_code"] or "")
                    for r in db.rows(
                        """SELECT DISTINCT UT.id AS unit_id, UT.code AS unit_code
                             FROM booth B
                             JOIN unit UT ON B.unit_id = UT.id
                            WHERE B.publication_id = %s""",
                        (config.UNIT_PUBLICATION_ID,),
                    )
                ]
            all_rows.extend(
                {"meetingId": mid, "location": code, "assembly": "", "parliament": "", "_uid": uid}
                for uid, code in unit_roll
                if uid not in covered_ids
            )
            continue
        if level not in roster_cache:
            roster_cache[level] = _level_roster(level)
        all_rows.extend(
            {"meetingId": mid, "location": name, "assembly": assembly, "parliament": parliament}
            for loc_id, name, assembly, parliament in roster_cache[level]
            if loc_id not in covered_ids
        )

    total = len(all_rows)
    page = all_rows[offset: offset + limit] if limit is not None else all_rows[offset:]
    unit_page = [r for r in page if "_uid" in r]
    if unit_page:
        lookup = _unit_lookup([r["_uid"] for r in unit_page])
        for r in unit_page:
            info = lookup.get(r["_uid"], {})
            r["location"] = info.get("code") or r["location"]
            r["assembly"] = info.get("assembly", "")
            r["parliament"] = info.get("parliament", "")
            del r["_uid"]
    for r in page:
        r.pop("_uid", None)
    return {"total": total, "limit": limit, "offset": offset, "rows": page}


@router.get("/schedules/pc-completed")
def pc_completed_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`is_conducted = 'Y'` — the same rows PC Status' Completed sums."""
    return _conducted_status_rows(meeting_ids, "is_conducted = 'Y'", limit=limit, offset=offset)


@router.get("/schedules/pc-not-completed")
def pc_not_completed_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`is_conducted IS NULL OR 'N'` — PC Status' Not conducted, the combined
    figure. Broader than `/pc-not-updated`, which is NULL alone."""
    return _conducted_status_rows(
        meeting_ids, "is_conducted IS NULL OR is_conducted = 'N'", limit=limit, offset=offset
    )


@router.get("/schedules/pc-not-updated")
def pc_not_updated_schedules(
    meeting_ids: str = Query(..., description="Comma-separated meeting ids"),
    limit: int | None = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`is_conducted IS NULL` — never touched, kept apart from an explicit 'N'."""
    return _conducted_status_rows(meeting_ids, "is_conducted IS NULL", limit=limit, offset=offset)


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
def schedule_summary(
    meeting_id: str,
    limit: int | None = Query(None, ge=1, le=5000, description="Page size; omit for all rows"),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """The App & PC summary panel — one row per `meeting_conducted_status`.

    MCS is primary. Schedules are fetched separately and merged in Python
    (a SQL CAST join on Unit-scale row counts times out). Unit name resolution
    drives off `unit` + a scoped booth subquery, chunked — not a full booth
    scan. Pass ``limit``/``offset`` so the UI can paint the first page while
    the rest loads (Unit meetings run to ~8k rows).
    """
    # Cheap level peek so Unit can take the fast path before loading names.
    meta = db.one(
        """SELECT l.level_name
             FROM meetings m
             LEFT JOIN meeting_levels l ON l.id = m.meeting_level_id
            WHERE m.id = %s""",
        (meeting_id,),
    )
    if meta is None:
        raise HTTPException(status_code=404, detail="Unknown meeting")
    level = adapt.level_code(meta["level_name"])

    def _mcs() -> list[dict[str, Any]]:
        sql = """SELECT c.meeting_conducted_status_id AS id, c.location_id,
                        c.is_conducted, mr.remarks_category_id, mr.remarks
                   FROM meeting_conducted_status c
                   LEFT JOIN meeting_remark mr
                          ON mr.meeting_conducted_status_id = c.meeting_conducted_status_id
                  WHERE c.meeting_id = %s
                  ORDER BY c.meeting_conducted_status_id"""
        args: list[Any] = [meeting_id]
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            args.extend([limit, offset])
        return db.rows(sql, tuple(args))

    def _total() -> int:
        return int(
            db.scalar(
                "SELECT COUNT(*) FROM meeting_conducted_status WHERE meeting_id = %s",
                (meeting_id,),
            )
            or 0
        )

    def _schedules_for(ids: list[str]) -> dict[str, dict[str, Any]]:
        """Schedules for this page's locations only — indexed `entity_id IN`,
        not a full-meeting scan (that was ~2s on every Unit page)."""
        if not ids:
            return {}
        nums: list[int] = []
        for x in ids:
            try:
                nums.append(int(x))
            except (TypeError, ValueError):
                continue
        if not nums:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(nums), 800):
            part = nums[i : i + 800]
            for s in db.rows(
                f"""SELECT entity_id, status, location_text, assembly_id
                      FROM meeting_schedules
                     WHERE meeting_id = %s AND entity_id IN ({db.placeholders(part)})""",
                (meeting_id, *part),
            ):
                if s["entity_id"] is not None:
                    out[str(s["entity_id"])] = s
        return out

    def _schedules_all() -> dict[str, dict[str, Any]]:
        return {
            str(s["entity_id"]): s
            for s in db.rows(
                """SELECT entity_id, status, location_text, assembly_id
                     FROM meeting_schedules WHERE meeting_id = %s""",
                (meeting_id,),
            )
            if s["entity_id"] is not None
        }

    if limit is not None:
        rows, total = db.parallel(_mcs, _total)
    else:
        rows = _mcs()
        total = len(rows)

    if not rows and offset == 0:
        return {"total": 0, "rows": []}

    loc_ids = list({str(r["location_id"]) for r in rows if r["location_id"] is not None})

    # Page-scoped schedule + name lookups run together after we know loc_ids.
    if level == "Unit" and loc_ids:
        schedules, unit_lookup = db.parallel(
            lambda: _schedules_for(loc_ids),
            lambda: _unit_lookup(loc_ids),
        )
    elif limit is not None:
        schedules = _schedules_for(loc_ids)
        unit_lookup = {}
    else:
        schedules = _schedules_all()
        unit_lookup = {}

    assembly_names: dict[str, str] = {}
    ac_parliament: dict[str, str] = {}
    parliament_names: dict[str, str] = {}
    mandal_locations: dict[str, str] = {}
    mandal_assembly: dict[str, str] = {}
    mandal_parliament: dict[str, str] = {}

    if level == "Unit":
        pass  # unit_lookup already filled above
    elif level == "AC" and loc_ids:
        for r in db.rows(
            f"""SELECT a.id, a.name, p.parliament_name
                  FROM assembly a
                  LEFT JOIN parliament p ON p.id = a.parliament_id
                 WHERE a.id IN ({db.placeholders(loc_ids)})""",
            tuple(loc_ids),
        ):
            assembly_names[str(r["id"])] = r["name"] or ""
            ac_parliament[str(r["id"])] = r["parliament_name"] or ""
    elif level == "PC" and loc_ids:
        for r in db.rows(
            f"SELECT id, parliament_name FROM parliament WHERE id IN ({db.placeholders(loc_ids)})",
            tuple(loc_ids),
        ):
            parliament_names[str(r["id"])] = r["parliament_name"] or ""
    elif level == "Mandal":
        mandal_locations = {
            str(r["location_id"]): r["location_name"] or ""
            for r in db.rows(_COMMITTEE_LOCATIONS)
            if r["location_id"] is not None
        }
        if loc_ids:
            for r in db.rows(
                f"SELECT id, name FROM mandal WHERE id IN ({db.placeholders(loc_ids)})",
                tuple(loc_ids),
            ):
                mandal_locations.setdefault(str(r["id"]), r["name"] or "")
            for r in db.rows(
                f"SELECT id, town_name FROM town WHERE id IN ({db.placeholders(loc_ids)})",
                tuple(loc_ids),
            ):
                mandal_locations.setdefault(str(r["id"]), r["town_name"] or "")
        ac_ids = list({
            str(schedules[str(r["location_id"])]["assembly_id"])
            for r in rows
            if r["location_id"] is not None
            and str(r["location_id"]) in schedules
            and schedules[str(r["location_id"])]["assembly_id"] is not None
        })
        if ac_ids:
            for r in db.rows(
                f"""SELECT a.id, a.name, p.parliament_name
                      FROM assembly a
                      LEFT JOIN parliament p ON p.id = a.parliament_id
                     WHERE a.id IN ({db.placeholders(ac_ids)})""",
                tuple(ac_ids),
            ):
                mandal_assembly[str(r["id"])] = r["name"] or ""
                mandal_parliament[str(r["id"])] = r["parliament_name"] or ""

    def row_out(r: dict[str, Any]) -> dict[str, Any]:
        loc = str(r["location_id"]) if r["location_id"] is not None else ""
        sched = schedules.get(loc, {})
        location_text = sched.get("location_text") or ""
        if level == "PC":
            location = parliament_names.get(loc) or location_text
            assembly, parliament = "", ""
        elif level == "AC":
            location = assembly_names.get(loc) or location_text
            assembly, parliament = "", ac_parliament.get(loc, "")
        elif level == "Mandal":
            location = mandal_locations.get(loc) or location_text
            ac = str(sched["assembly_id"]) if sched.get("assembly_id") is not None else ""
            assembly = mandal_assembly.get(ac, "")
            parliament = mandal_parliament.get(ac, "")
        else:
            info = unit_lookup.get(loc, {})
            location = info.get("code") or location_text
            assembly = info.get("assembly", "")
            parliament = info.get("parliament", "")
        return {
            "id": r["id"],
            "parliament": parliament,
            "assembly": assembly,
            "location": location,
            "appConducted": sched.get("status") in (1, 2),
            "pcConducted": r["is_conducted"] == "Y",
            "conductedStatusId": r["id"],
            "categoryId": r["remarks_category_id"],
            "remarks": r["remarks"] or "",
        }

    return {"total": total, "rows": [row_out(r) for r in rows]}


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
    agg_all, not_scheduled_all, pc_not_updated_all = db.parallel(
        lambda: _aggregates([str(row["id"])]),
        lambda: _not_scheduled_counts([row]),
        lambda: _pc_not_updated_counts([row]),
    )
    agg = agg_all.get(str(row["id"]), {})
    not_scheduled = not_scheduled_all.get(str(row["id"]), 0)
    pc_not_updated = pc_not_updated_all.get(str(row["id"]), 0)
    return adapt.meeting(row, agg, not_scheduled, pc_not_updated)


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
