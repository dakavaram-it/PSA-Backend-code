"""The assembly scoping: the predicates themselves, and that a narrow grant is
a strict subset of the state's own figures.

The pure half needs no database. The live half asserts the property that
matters — one assembly's numbers can never exceed the whole state's, and the
locations a scoped caller is shown are a subset of the ones they are granted.
"""

import base64
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app import access, auth, config, db
from app.access import Scope
from app.main import app


def _token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + 600},
        base64.b64decode(config.JWT_SECRET + "==="),
        algorithm=config.JWT_ALGORITHM,
    )


def _client(user_id: int) -> TestClient:
    return TestClient(app, headers={"Authorization": "Bearer " + _token(user_id)})


def db_up() -> bool:
    try:
        return db.scalar("SELECT 1") == 1
    except Exception:
        return False


live = pytest.mark.skipif(not db_up(), reason="party database unreachable")


# --- pure -------------------------------------------------------------------

def test_unrestricted_scope_adds_no_condition():
    """A whole-state grant must leave every query exactly as it was: `1 = 1`
    folds away, an id list would not."""
    everything = Scope(None)
    assert access.invitee(everything) == "1 = 1"
    assert access.schedules(everything, "Unit") == "1 = 1"
    assert access.conducted(everything, "PC") == "1 = 1"
    assert access.leader(everything) == "1 = 1"


def test_empty_grant_matches_nothing():
    """No grants is no data, never all of it — and never `IN ()`, which will
    not parse."""
    none = Scope([])
    assert access.invitee(none) == access.NOTHING
    assert access.schedules(none, "AC") == access.NOTHING


def test_each_level_reaches_its_own_column():
    """The three schedule rules, which are the whole reason `level` is a
    parameter: Unit and Mandal carry the assembly on the schedule row, AC's
    entity IS the assembly, and a PC expands to the assemblies inside it."""
    one = Scope([228])
    assert access.schedules(one, "Unit") == "s.assembly_id IN (228)"
    assert access.schedules(one, "Mandal") == "s.assembly_id IN (228)"
    assert access.schedules(one, "AC") == "CAST(s.entity_id AS CHAR) IN ('228')"
    assert "parliament_id" in access.schedules(one, "PC")


def test_ids_are_normalised_to_integers():
    """The lists are interpolated, not bound, so nothing but an int may reach
    the SQL."""
    assert Scope(["228", 228, 106]).nums == "106, 228"
    assert Scope([228]).strs == "'228'"
    with pytest.raises(ValueError):
        Scope(["228; DROP TABLE meetings"])


# --- live -------------------------------------------------------------------

@live
def test_a_narrow_grant_is_a_subset_of_the_state():
    """Same meetings, never larger figures. Run against a user holding one
    assembly and a user holding the state."""
    narrow_id = db.scalar(
        """SELECT user_id FROM dakavara_pa.user_constituency_access_info
            GROUP BY user_id HAVING COUNT(*) = 1 LIMIT 1"""
    )
    assert narrow_id is not None, "no single-assembly user to test with"
    assert auth.scope_for_user(1).unrestricted, "user 1 should hold a state grant"

    period = "?from=2026-01-01&to=2026-12-31"
    whole = {m["id"]: m for m in _client(1).get("/api/meetings" + period).json()}
    part = {m["id"]: m for m in _client(narrow_id).get("/api/meetings" + period).json()}

    assert part.keys() == whole.keys(), "scoping narrows figures, not the meeting list"
    for mid, row in part.items():
        for field in ("invitees", "attendees", "attendanceRecords", "resolutions"):
            assert row[field] <= whole[mid][field], (mid, field)
        assert row["units"]["total"] <= whole[mid]["units"]["total"], mid


@live
def test_a_scoped_caller_only_sees_granted_assemblies():
    narrow_id = db.scalar(
        """SELECT user_id FROM dakavara_pa.user_constituency_access_info
            GROUP BY user_id HAVING COUNT(*) = 1 LIMIT 1"""
    )
    granted = {str(i) for i in auth.scope_for_user(narrow_id).ids}
    rows = _client(narrow_id).get("/api/assemblies").json()["rows"]
    assert {str(r["constituencyId"]) for r in rows} == granted


def test_every_route_needs_a_token():
    anonymous = TestClient(app)
    assert anonymous.get("/api/assemblies").status_code == 401
    assert anonymous.get("/api/meetings?from=2026-01-01&to=2026-01-31").status_code == 401
    # the liveness probe stays open, or nothing can health-check this service
    assert anonymous.get("/health").status_code == 200


@live
def test_programme_leader_routes_refuse_a_leader_outside_the_grant():
    """The `/programs/leaders/{id}/…` routes are keyed by a leader rather than
    by an assembly, so the leader is the thing that has to be granted: one
    outside the grant reads as unknown on both GETs and takes no write.

    The write bodies name a programme and an entry that cannot exist, so a
    broken gate fails this test without leaving a row behind in the live
    party database.
    """
    narrow_id = db.scalar(
        """SELECT user_id FROM dakavara_pa.user_constituency_access_info
            GROUP BY user_id HAVING COUNT(*) = 1 LIMIT 1"""
    )
    granted = Scope(auth.scope_for_user(narrow_id).ids)
    outsider = db.scalar(
        f"""SELECT leader_id FROM {config.PARTY_TRACK_DB}.leader
             WHERE constituency_id NOT IN ({granted.nums}) AND is_deleted = 'N'
             LIMIT 1"""
    )
    assert outsider is not None, "no leader outside the grant to test with"

    client = _client(narrow_id)
    base = f"/api/programs/leaders/{outsider}"
    assert client.get(f"{base}/meetings?year=2026&month=1").json() == []
    assert client.get(f"{base}/log-entries?program_id=999999&year=2026&month=1").json() == []
    assert client.put(f"{base}/meetings/1/remarks", json={"remarks": "x"}).status_code == 404
    assert client.post(
        f"{base}/log-entries", json={"programId": 999999, "date": "2026-01-01", "remarks": "x"}
    ).status_code == 404
    assert client.delete(f"{base}/log-entries/999999999").status_code == 404
