"""Two layers: the row shaping, which is pure, and the live endpoints.

The live half is skipped when the party database is unreachable, so the suite
still runs on a laptop with no VPN. Nothing here writes — the remark tests use
the paths that refuse before touching `feedback_comment` or `meeting_remark`.
"""

import base64
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app import adapt, config, db
from app.main import app
from app.routers.programs import _leader_with_role_sql


def _state_token() -> str:
    """A session for a user the portal has granted the whole state.

    Every route is scoped to the caller's own assemblies now, so the assertions
    below — which are about state-wide totals — need a caller the scope does not
    narrow. A row in `user_state_access_info` is exactly that grant, and user 1
    holds one. The token is minted here rather than fetched from the portal:
    this service only ever verifies one, and the secret is the same on both
    sides.
    """
    now = int(time.time())
    return jwt.encode(
        {"sub": "1", "iat": now, "exp": now + 600},
        base64.b64decode(config.JWT_SECRET + "==="),
        algorithm=config.JWT_ALGORITHM,
    )


client = TestClient(app, headers={"Authorization": "Bearer " + _state_token()})


def db_up() -> bool:
    try:
        return db.scalar("SELECT 1") == 1
    except Exception:
        return False


live = pytest.mark.skipif(not db_up(), reason="party database unreachable")


# --- pure -------------------------------------------------------------------

def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_pct_rounds_half_up():
    """62.5% has to read 63, the same as Math.round in lib/format.js."""
    assert adapt.pct(5, 8) == 63
    assert adapt.pct(0, 0) == 100


def test_level_code():
    assert adapt.level_code("Mandal / Town / Division") == "Mandal"
    assert adapt.level_code(None) == "Unit"


def test_meeting_split_and_funnel():
    m = adapt.meeting(
        {"id": 15, "title": "t", "meeting_date": None, "level_name": "AC"},
        {"invitees": 100, "attendees": 40, "attendanceRecords": 250,
         "units": 10, "unitsCompleted": 4, "resolutions": 3, "feedbackTaken": 15,
         "pcTotal": 9, "pcConducted": 3, "pcNull": 6},
        pc_not_updated=7,
    )
    assert (m["attendees"], m["absent"]) == (40, 60)
    # the attendance-record count exceeds the invitee list and must never be
    # folded into the split
    assert m["attendanceRecords"] == 250
    assert m["units"]["started"] == m["units"]["completed"] == 4
    assert m["units"]["notConducted"] == 6
    assert (m["feedbackPending"], m["completion"]) == (45, 25)
    # `notConducted` is `IS NULL` alone; an explicit 'N' counts as neither
    # conducted nor not-conducted, so this only feet back to `pcTotal` when
    # (as here) every non-conducted row is NULL and none is 'N'.
    assert (m["pc"]["conducted"], m["pc"]["notConducted"]) == (3, 6)
    assert m["pc"]["conducted"] + m["pc"]["notConducted"] == m["pc"]["total"]
    # `notUpdated` is the caller-supplied roster-absence figure, not derived
    # from `agg` at all — a location with no conducted-status row is outside
    # `pcTotal` the same way a never-scheduled one is outside `units.total`.
    assert m["pc"]["notUpdated"] == 7
    # Schedule-row total for the card; backup Not Updated is arithmetic.
    assert m["totalMeetings"] == 9
    assert m["pc"]["notUpdatedBackup"] == 0  # 9 − 9


def test_meeting_pc_not_conducted_excludes_explicit_n():
    """An explicit 'N' row is neither conducted nor not-conducted under the
    IS-NULL definition — `pcTotal` can exceed `conducted + notConducted`."""
    m = adapt.meeting(
        {"id": 16, "title": "t", "meeting_date": None, "level_name": "AC"},
        {"pcTotal": 9, "pcConducted": 3, "pcNull": 4},
    )
    assert (m["pc"]["conducted"], m["pc"]["notConducted"]) == (3, 4)
    assert m["pc"]["conducted"] + m["pc"]["notConducted"] < m["pc"]["total"]


def test_meeting_includes_pc_remarks_count():
    m = adapt.meeting(
        {"id": 15, "title": "t", "meeting_date": None, "level_name": "AC"},
        {"invitees": 100, "attendees": 40, "attendanceRecords": 250,
         "units": 10, "unitsCompleted": 4, "resolutions": 3, "feedbackTaken": 15,
         "pcTotal": 9, "pcConducted": 3, "pcRemarks": 7},
    )
    assert m["pcRemarks"] == 7


def test_meeting_with_no_conducted_status_rows_reports_pc_as_none():
    """A `GROUP BY` never yields a zero-count group, so a missing pcTotal must
    mean 'not tracked yet' — never a fake 0/0 that reads as 'zero conducted'."""
    m = adapt.meeting(
        {"id": 13, "title": "t", "meeting_date": None, "level_name": "Unit Level"},
        {"invitees": 10, "attendees": 2, "attendanceRecords": 2,
         "units": 1, "unitsCompleted": 0, "resolutions": 0, "feedbackTaken": 0},
    )
    assert m["pc"] is None
    assert m["totalMeetings"] == 0


def test_programs_are_still_unwired():
    res = client.get("/api/programs?from=2026-06-01&to=2026-06-30")
    assert res.status_code == 501


# --- live -------------------------------------------------------------------

@live
def test_meetings_list_is_counted():
    rows = client.get("/api/meetings?from=2026-06-01&to=2026-08-31").json()
    assert rows, "no committee meetings in the seeded range"
    for m in rows:
        assert m["absent"] == m["invitees"] - m["attendees"]
        assert m["attendees"] <= m["invitees"]


@live
def test_rollup_totals_match_the_member_count():
    """The rollup describes the whole list, not the page the table holds."""
    roll = client.get("/api/meetings/15/rollup").json()
    page = client.get("/api/meetings/15/members?limit=1").json()
    assert roll["totals"]["invited"] == page["total"]
    assert sum(r["invited"] for r in roll["byPc"]) == roll["totals"]["invited"]
    assert sum(r["invited"] for r in roll["byAc"]) == roll["totals"]["invited"]


@live
def test_schedule_summary_matches_meeting_pc_total():
    """One row per MCS — must foot to the same figure `pc.total` sums."""
    data = client.get("/api/meetings/18/schedule-summary").json()
    meeting = client.get("/api/meetings/18").json()
    assert meeting["pc"] is not None
    assert data["total"] == len(data["rows"]) == meeting["pc"]["total"]
    assert sum(r["pcConducted"] for r in data["rows"]) == meeting["pc"]["conducted"]


@live
def test_schedule_summary_carries_the_remark_fields():
    data = client.get("/api/meetings/18/schedule-summary").json()
    assert data["rows"], "no MCS rows for meeting 18"
    row = data["rows"][0]
    assert {"conductedStatusId", "categoryId", "remarks"} <= row.keys()
    # Every summary row is MCS-backed, so Status Update is always wired.
    assert row["conductedStatusId"] is not None


@live
def test_pc_remarks_schedules_matches_the_meeting_sum():
    """Row-level `meeting_remark` detail must foot to `pcRemarks` per meeting."""
    data = client.get("/api/meetings/schedules/pc-remarks?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(meetings[i]["pcRemarks"] for i in ("13", "22", "26") if i in meetings)
    assert data["total"] == len(data["rows"]) == expected


@live
def test_conducted_remark_rejects_an_unknown_status_row():
    res = client.put("/api/meetings/conducted-status/00000000/remark", json={"remarks": "x"})
    assert res.status_code == 404


def test_conducted_remark_length_is_capped():
    """Rejected by the model before any query runs, database or not."""
    over = "x" * (config.MAX_REMARKS_CHARS + 1)
    assert client.put(
        "/api/meetings/conducted-status/00000000/remark", json={"remarks": over}
    ).status_code == 422


@live
def test_remarks_refuse_a_present_member():
    mid = db.scalar(
        """SELECT i.membership_id FROM meeting_invitee i
             JOIN (SELECT DISTINCT meeting_id, mid FROM meeting_attendance) a
               ON a.meeting_id = 15 AND a.mid = i.membership_id
            WHERE i.meeting_id = '15' LIMIT 1"""
    )
    res = client.put(f"/api/meetings/15/members/{mid}/remarks", json={"remarks": "x"})
    assert res.status_code == 400


@live
def test_mandal_town_division_total_matches_row_count():
    data = client.get("/api/committees/mandal-town-division").json()
    assert data["total"] == len(data["rows"])
    if data["rows"]:
        assert data["rows"][0]["locationName"]


@live
def test_assemblies_cover_the_whole_state():
    """175 ACs, the same figure the AC/PC join in meetings.py relies on."""
    data = client.get("/api/assemblies").json()
    assert data["total"] == len(data["rows"]) == 175


@live
def test_parliaments_cover_the_whole_state():
    data = client.get("/api/assemblies/parliaments").json()
    assert data["total"] == len(data["rows"]) == 25


@live
def test_units_total_matches_row_count():
    """The grouped query must not fan out beyond COUNT(DISTINCT unit.id)."""
    data = client.get("/api/units").json()
    assert data["total"] == len(data["rows"])
    assert data["total"] > 0


@live
def test_remarks_categories_are_a_real_roster():
    data = client.get("/api/remarks-categories").json()
    assert data == [{"id": 1, "name": "Cat-1"}, {"id": 2, "name": "Cat-2"}]


@live
def test_program_roles_are_a_real_roster():
    """`/roles` narrows `party_track.role` to roles with both an active
    leader and a `program_role` mapping — member designations, not
    `mytdp.role`'s committee-meeting tiers, and not every row in `role`
    qualifies (MLC has program_role rows but no leaders, so it drops out)."""
    data = client.get("/api/programs/roles").json()
    assert data, "no rows matched leader + program_role"
    names = {r["name"] for r in data}
    assert {"Minister", "MLA", "MP", "Mandal President"} <= names
    assert "MLC" not in names


@live
def test_program_activities_are_a_real_roster():
    """`/activities` reads `party_track.program` — the same table
    `/activity-summary` joins through `program_role` — not `party_track.activity`,
    a separate scoring system this feature does not use."""
    data = client.get("/api/programs/activities").json()
    assert data, "no rows in party_track.program"
    names = {r["name"] for r in data}
    assert {"Calendar Meeting", "Pedala Sevalo"} <= names


@live
def test_role_summary_reads_the_live_leader_roster():
    """Total/Members come straight off `leader`, independent of the month —
    Minister has no soft-deleted rows, so the two must match exactly."""
    data = client.get("/api/programs/role-summary?year=2026&month=7").json()
    assert data, "no rows in party_track.role"
    minister = next(r for r in data if r["role"] == "Minister")
    assert minister["total"] == minister["members"] > 0
    for r in data:
        assert r["members"] == r["updated"] + r["notUpdated"]


@live
def test_role_summary_is_the_same_roster_regardless_of_month():
    """Total/Members are roster figures, not period figures — unlike Updated,
    they must not change just because the month has no `month` row at all."""
    seeded = client.get("/api/programs/role-summary?year=2026&month=7").json()
    unseeded = client.get("/api/programs/role-summary?year=2026&month=8").json()
    seeded_totals = {r["role"]: r["total"] for r in seeded}
    unseeded_totals = {r["role"]: r["total"] for r in unseeded}
    assert seeded_totals == unseeded_totals


@live
def test_activity_summary_only_carries_real_program_role_pairings():
    """One row per `program_role` pairing for a role that actually has an
    active leader, not a full role x programme cross-product — a role never
    appears for a programme it isn't linked to, and a role nobody holds
    (MLC has program_role rows but no leaders) never appears at all.

    "Active leader" is checked against `_leader_with_role_sql`, not raw
    `leader.role_id` — a Minister who is also an MLA (`leader_role` rows
    `1,2`) must count as an active leader for both, and raw `role_id` alone
    only sees the first."""
    data = client.get("/api/programs/activity-summary?year=2026&month=7").json()
    pairings = {(r["role"], r["activity"]) for r in data}
    linked = {
        (r["role_name"], r["program_name"])
        for r in db.rows(
            f"""SELECT DISTINCT r.role_name, p.program_name
                  FROM {config.PARTY_TRACK_DB}.program_role pr
                  JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = pr.role_id
                  JOIN {config.PARTY_TRACK_DB}.program p ON p.program_id = pr.program_id
                 WHERE (pr.is_deleted IS NULL OR pr.is_deleted = 'N')
                   AND EXISTS (
                         SELECT 1 FROM {_leader_with_role_sql()} l
                          WHERE l.role_id = pr.role_id AND l.is_deleted = 'N'
                            AND l.party_id = {config.LEADER_PARTY_ID}
                       )"""
        )
    }
    assert pairings == linked


@live
def test_program_leaders_cover_the_whole_role_roster():
    """Every active leader in the role gets a row, even with zero
    leader_program_activity rows recorded — 0/0, not left out.

    The expected count is `_leader_with_role_sql`, not raw `leader.role_id`
    — Minister happens to match either way today, but MLA would not (all
    21 active Ministers are also MLAs in `leader_role`, so raw `role_id`
    alone undercounts MLA by 21). Also scoped to `config.LEADER_PARTY_ID`,
    same as the endpoint — `leader` holds a handful of role_id=1 rows for
    other parties that `/leaders` never returns."""
    role = db.one(
        f"SELECT role_id FROM {config.PARTY_TRACK_DB}.role WHERE role_name = 'Minister'"
    )
    expected = db.scalar(
        f"""SELECT COUNT(*) FROM {_leader_with_role_sql()} l
             WHERE l.role_id = %s AND l.is_deleted = 'N' AND l.party_id = %s""",
        (role["role_id"], config.LEADER_PARTY_ID),
    )
    data = client.get(
        f"/api/programs/leaders?role_id={role['role_id']}&activity_id=1&year=2026&month=7"
    ).json()
    assert len(data) == expected
    for r in data:
        assert r["participated"] >= 0 and r["completed"] >= 0


@live
def test_conducted_schedules_matches_the_units_completed_sum():
    """`status IN (1, 2)` row-level detail must foot to the summed figure."""
    data = client.get("/api/meetings/schedules/conducted?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(meetings[i]["units"]["completed"] for i in ("13", "22", "26") if i in meetings)
    assert data["total"] == len(data["rows"]) == expected


@live
def test_not_updated_schedules_matches_the_units_notconducted_sum():
    """`status = 0` row-level detail must foot to the level's summed figure."""
    data = client.get("/api/meetings/schedules/not-updated?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(meetings[i]["units"]["notConducted"] for i in ("13", "22", "26") if i in meetings)
    assert data["total"] == len(data["rows"]) == expected


@live
def test_pc_completed_schedules_matches_the_pc_conducted_sum():
    """`is_conducted = 'Y'` row-level detail must foot to the summed figure."""
    data = client.get("/api/meetings/schedules/pc-completed?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(
        meetings[i]["pc"]["conducted"] for i in ("13", "22", "26") if i in meetings and meetings[i]["pc"]
    )
    assert data["total"] == len(data["rows"]) == expected


@live
def test_pc_not_completed_schedules_matches_the_pc_notconducted_sum():
    """`is_conducted IS NULL` must foot to the summed `pc.notConducted` figure."""
    data = client.get("/api/meetings/schedules/pc-not-completed?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(
        meetings[i]["pc"]["notConducted"] for i in ("13", "22", "26") if i in meetings and meetings[i]["pc"]
    )
    assert data["total"] == len(data["rows"]) == expected


@live
def test_pc_never_updated_schedules_matches_the_pc_notupdated_sum():
    """Roster locations with no `meeting_conducted_status` row at all must
    foot to the summed `pc.notUpdated` figure — the PC-side twin of
    `test_units_total_matches_row_count`'s App-side check."""
    data = client.get("/api/meetings/schedules/pc-never-updated?meeting_ids=13,22,26").json()
    meetings = {
        m["id"]: m for m in client.get("/api/meetings?from=2026-01-01&to=2026-12-31").json()
    }
    expected = sum(
        meetings[i]["pc"]["notUpdated"] for i in ("13", "22", "26") if i in meetings and meetings[i]["pc"]
    )
    assert data["total"] == len(data["rows"]) == expected


@live
def test_remarks_reject_an_unknown_member():
    res = client.put("/api/meetings/15/members/00000000/remarks", json={"remarks": "x"})
    assert res.status_code == 404


def test_remarks_length_is_capped():
    """Rejected by the model before any query runs, database or not."""
    over = "x" * (config.MAX_REMARKS_CHARS + 1)
    assert client.put(
        "/api/meetings/15/members/00000000/remarks", json={"remarks": over}
    ).status_code == 422
