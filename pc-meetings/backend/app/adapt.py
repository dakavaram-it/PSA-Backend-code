"""Database rows -> the vocabulary the React app already speaks.

The field names on the left are `mytdp`'s; the ones on the right are the JSON
keys `App.jsx` reads. Anything the schema does not carry (a meeting has no
single venue — it runs across every unit in the state) is ``None`` here rather
than invented, and the UI omits it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# `meeting_levels.level_name` for meeting_type_id 1, mapped to the short codes
# the UI filters and colours on (`--lvl-unit`, `badge-unit`, …).
LEVEL_BY_NAME = {
    "Unit Level": "Unit",
    "Mandal / Town / Division": "Mandal",
    "AC": "AC",
    "PC": "PC",
}


def level_code(level_name: str | None) -> str:
    return LEVEL_BY_NAME.get(level_name or "", "Unit")


def pct(part: int, whole: int) -> int:
    """Half-up, matching Math.round in the UI — 62.5% has to read 63 in both."""
    return int(part / whole * 100 + 0.5) if whole else 100


def num(v: Any) -> int:
    """SUM() comes back as Decimal, COUNT() as int, a missing group as None."""
    return int(v) if v is not None else 0


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def meeting(
    row: dict[str, Any], agg: dict[str, Any], not_scheduled: int = 0, pc_not_updated: int = 0
) -> dict[str, Any]:
    """One meeting card.

    ``agg`` carries the counted figures for this meeting: invitees, attendees,
    attendance records, resolutions, remarks taken, the App-side schedule
    funnel and the PC in-charge's real conducted/not-conducted split.

    The schedule funnel and PC split (`units`/`unitsCompleted`/`pcTotal`/
    `pcConducted`/`pcNull` on ``agg``) are already roster-scoped by the
    caller (`_schedule_stats`/`_conducted_stats`) — a `meeting_schedules` or
    `meeting_conducted_status` row whose location has since fallen out of
    the current roster (an old-publication unit, a mandal no longer
    enrolled) isn't counted as Conducted or Not Conducted here, the same way
    it was already excluded from ``not_scheduled``/``pc_not_updated`` below.
    Without that, the three App buckets (and the three PC buckets) could sum
    to more than the roster size by however many such rows a meeting happens
    to carry.

    ``not_scheduled`` and ``pc_not_updated`` are already the finished figures
    — how many of this meeting's own level's roster locations (units,
    mandals/towns, ACs or PCs) have no `meeting_schedules` row, respectively
    no `meeting_conducted_status` row, at all — computed by the caller rather
    than a flat roster size here, since a plain "roster size minus this
    meeting's own row count" silently undercounts whenever a meeting also
    carries a row outside the roster.
    """
    invitees = num(agg.get("invitees"))
    attendees = num(agg.get("attendees"))
    absent = invitees - attendees
    taken = num(agg.get("feedbackTaken"))

    total_units = num(agg.get("units"))
    completed = num(agg.get("unitsCompleted"))

    pc_total = num(agg.get("pcTotal"))
    pc_conducted = num(agg.get("pcConducted"))
    pc_null = num(agg.get("pcNull"))

    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "level": level_code(row.get("level_name")),
        "levelName": row.get("level_name") or "",
        "meetingType": "Committee",
        "date": _iso(row.get("meeting_date")),
        "endDate": None,
        "days": 1,
        "invitees": invitees,
        # `meeting_attendance` holds one row per attendance record, and for every
        # meeting most of them belong to people who were never on the invitee
        # list — for meeting 15, 12,156 distinct ids against 8,303 invitees. It
        # is reported as its own figure and never subtracted from anything.
        "attendanceRecords": num(agg.get("attendanceRecords")),
        # The real split: invitees with at least one attendance row.
        "attendees": attendees,
        "absent": absent,
        "units": {
            # Roster-scoped (see the docstring above) — a schedule row for a
            # location no longer on the current roster isn't in this total.
            "total": total_units,
            # `meeting_schedules.status` is a flag, not a lifecycle: 1 or 2 is
            # held, 0 is not. There is no in-progress state to report, so
            # `onGoing` stays 0 and `started` equals `completed`, which keeps
            # the funnel's own arithmetic (started = onGoing + completed) true.
            "started": completed,
            "onGoing": 0,
            "completed": completed,
            "notConducted": total_units - completed,
        },
        # Roster locations this meeting never scheduled at all — outside the
        # `units` funnel above on purpose: `units.total` already IS "what got
        # scheduled", so folding this in would break `total = started +
        # notConducted`, the identity `StateBand` draws the funnel from.
        "notScheduled": not_scheduled,
        # `meeting_conducted_status.is_conducted`: 'Y' is conducted, `IS NULL`
        # is Not conducted — the row exists but the PC in-charge hasn't marked
        # it. An explicit 'N' is its own, rare state and counts as neither;
        # `conducted + notConducted` can therefore sit just under `total` on a
        # meeting that carries one. A `GROUP BY` never yields a zero-count
        # group, so `pc_total == 0` can only mean this meeting has no rows
        # there at all — `None` rather than a fake 0/0, so the UI can tell
        # "not tracked yet" from "tracked, zero conducted so far".
        "pc": None if pc_total == 0 else {
            "total": pc_total,
            "conducted": pc_conducted,
            "notConducted": pc_null,
            # Roster locations with no `meeting_conducted_status` row at all
            # — the PC-side twin of `notScheduled` above, outside `total` on
            # purpose the same way `notScheduled` sits outside `units.total`.
            "notUpdated": pc_not_updated,
        },
        # Rows in `meeting_remark` carrying real text, joined off the PC
        # in-charge's own conducted-status rows above.
        "pcRemarks": num(agg.get("pcRemarks")),
        "resolutions": num(agg.get("resolutions")),
        "feedbackTaken": taken,
        "feedbackPending": max(absent - taken, 0),
        "completion": pct(taken, absent),
    }


def member(row: dict[str, Any]) -> dict[str, Any]:
    """One invitee of a meeting."""
    present = bool(row.get("present"))
    remarks = row.get("remarks") or ""
    return {
        "mid": row.get("membership_id") or "",
        "name": row.get("member_name") or "",
        "mobile": row.get("mobile_no") or "",
        "pc": row.get("pc") or "",
        "ac": row.get("ac") or "",
        # a member's level is their own committee tier (Booth, Village, Unit…),
        # not the meeting's — left as the table words it
        "levelName": row.get("level_name") or "",
        "committee": row.get("committee_name") or "",
        "designation": row.get("role_name") or "",
        "present": present,
        "presentOn": [present],
        "feedback": bool(remarks),
        "remarks": remarks,
        "capturedBy": row.get("captured_by") or "",
    }


def posture(row: dict[str, Any], **keys: str) -> dict[str, Any]:
    """One aggregate line: how much of this slice has been chased down."""
    invited, attended = num(row["invited"]), num(row["attended"])
    absent, captured = invited - attended, num(row["captured"])
    return {
        **keys,
        "invited": invited,
        "attended": attended,
        "absent": absent,
        "captured": captured,
        "pending": absent - captured,
        "attendance": pct(attended, invited),
        "completion": pct(captured, absent),
    }
