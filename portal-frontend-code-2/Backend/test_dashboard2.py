"""Backend/test_dashboard2.py — the invariants Dashboard 2's numbers rest on.

Runs against the live dakavara_pa database, like ../../portal-frontend-code/Backend's own
tests. It asserts relationships between endpoints rather than fixed figures, so it stays
true as candidates are proposed and confirmed — the only hard numbers here are the position
counts, which are seeded configuration and do not move on their own.

    cd Backend && python test_dashboard2.py       (or: pytest test_dashboard2.py)
"""
import sys

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

# ZPTC Member — 671 positions, the post with the most proposed names in the live data, and
# the only one whose locations span every parliament constituency in the state.
ZPTC = dict(mainElectionTypeId=2, proposalElectionTypeId=1, proposalRoleId=3)
ZPTC_POSITIONS = 671

# The 15 assemblies the caller in the brief scopes to, and two parliaments that cover most
# of them.
ASSEMBLY_IDS = "111,127,133,134,135,136,137,140,141,354,355,356,357,358,368"
PARLIAMENT_IDS = "464,508"


def get(path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:400]}"
    return response.json()


def test_summary_covers_every_position():
    """The summary's rows partition proposal_position: nothing counted twice, nothing lost."""
    body = get("/api/dashboard2/positionSummary")
    assert body["totals"]["total_locations"] == sum(
        p["total_locations"] for p in body["positions"]
    )
    zptc = [
        p
        for p in body["positions"]
        if p["proposal_role_id"] == 3 and p["proposal_election_type_id"] == 1
    ]
    assert len(zptc) == 1, "role + election type must identify exactly one row"
    assert zptc[0]["total_locations"] == ZPTC_POSITIONS


def test_geo_halves_add_back_to_the_position_total():
    """The property the geo table promises: each column sums down to the position total.

    This is what queries.ASSEMBLY_EXPR's one-assembly-per-position rule buys. A position
    attributed to two assemblies would break this and nothing else would notice.
    """
    geo = get("/api/dashboard2/geoBreakdown", **ZPTC)
    assert sum(r["total_locations"] for r in geo["parliaments"]) == ZPTC_POSITIONS
    assert sum(r["total_locations"] for r in geo["assemblies"]) == ZPTC_POSITIONS


def test_reservation_split_adds_back_to_the_position_total():
    body = get("/api/dashboard2/reservationSummary", **ZPTC)
    assert sum(r["total_locations"] for r in body["reservations"]) == ZPTC_POSITIONS
    assert body["totals"]["total_locations"] == ZPTC_POSITIONS


def test_counters_are_internally_consistent():
    """not_started + started == total, and each pending figure is the gap it names."""
    for row in get("/api/dashboard2/positionSummary")["positions"]:
        assert row["not_started"] + row["started"] == row["total_locations"]
        assert row["pending_confirmation"] == row["started"] - row["confirmed"]
        assert row["pending_nomination"] == row["confirmed"] - row["nominated"]
        # A confirmed location is a started one; a nominated one is confirmed. If this ever
        # fails, queries.STAGE_EXPR and these counters have stopped agreeing.
        assert row["confirmed"] <= row["started"] <= row["total_locations"]
        assert row["nominated"] <= row["confirmed"]


def test_stage_filter_partitions_the_locations():
    """Every location is at exactly one stage, so the per-stage counts sum to the total."""
    total = get("/api/dashboard2/locations", **ZPTC, limit=1)["total"]
    assert total == ZPTC_POSITIONS
    per_stage = [
        get("/api/dashboard2/locations", **ZPTC, stage=s, limit=1)["total"] for s in range(7)
    ]
    assert sum(per_stage) == total
    # Stages 4-6 have no source table, so nothing can be at them.
    assert per_stage[4:] == [0, 0, 0]


def test_locations_page_carries_its_own_names():
    """A location's `names` count matches the candidate rows attached to it."""
    page = get("/api/dashboard2/locations", **ZPTC, stage=1, limit=10)
    assert page["locations"], "no location has a proposed name - seed one and re-run"
    for location in page["locations"]:
        assert location["stage"] == 1
        assert location["stage_name"] == "Proposal received"
        assert len(location["candidates"]) == location["names"] > 0

    ids = [location["proposal_position_id"] for location in page["locations"]]
    standalone = get(
        "/api/dashboard2/locationCandidates", proposalPositionId=",".join(map(str, ids))
    )
    by_id = {g["proposal_position_id"]: g["candidates"] for g in standalone["locations"]}
    for location in page["locations"]:
        assert len(by_id[location["proposal_position_id"]]) == location["names"]


def test_scope_narrows_and_the_two_parameter_shapes_agree():
    """Assembly scope is a subset of state-wide, and comma vs repeated array match."""
    state_wide = get("/api/dashboard2/positionSummary")["totals"]["total_locations"]
    joined = get(
        "/api/dashboard2/positionSummary",
        userLocationLevelId=5,
        userLocationLevelValuesStr=ASSEMBLY_IDS,
    )
    repeated = get(
        "/api/dashboard2/positionSummary",
        userLocationLevelId=5,
        userLocationLevelValuesStr=ASSEMBLY_IDS.split(","),
    )
    assert joined["totals"] == repeated["totals"]
    assert joined["scope"]["resolvedAssemblyCount"] == 15
    assert 0 < joined["totals"]["total_locations"] < state_wide


def test_parliament_scope_expands_to_its_assemblies():
    body = get(
        "/api/dashboard2/positionSummary",
        userLocationLevelId=4,
        userLocationLevelValuesStr=PARLIAMENT_IDS,
    )
    assert body["scope"]["stateWide"] is False
    assert body["scope"]["resolvedAssemblyCount"] > 1
    assert body["totals"]["total_locations"] > 0


def test_unknown_level_is_rejected_and_junk_ids_do_not_widen_the_scope():
    assert (
        client.get(
            "/api/dashboard2/positionSummary",
            params={"userLocationLevelId": 3, "userLocationLevelValuesStr": "111"},
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/dashboard2/positionSummary",
            params={"userLocationLevelId": 5, "userLocationLevelValuesStr": "abc"},
        ).status_code
        == 400
    )
    # An id that resolves to nothing must stay empty, never fall back to the whole state.
    empty = get(
        "/api/dashboard2/positionSummary",
        userLocationLevelId=5,
        userLocationLevelValuesStr="99999999",
    )
    assert empty["scope"]["stateWide"] is False
    assert empty["totals"]["total_locations"] == 0


def test_pipeline_matches_the_summary_it_is_drawn_from():
    scope = dict(userLocationLevelId=5, userLocationLevelValuesStr=ASSEMBLY_IDS)
    steps = get("/api/dashboard2/pipeline", **scope)
    totals = get("/api/dashboard2/positionSummary", **scope)["totals"]
    assert steps["totals"] == totals
    assert steps["steps"][0]["of"] == totals["total_locations"]
    assert steps["steps"][0]["done"] == totals["started"]
    assert [s["available"] for s in steps["steps"]] == [True, True, True, False, False, False]


def test_lookups_are_the_reference_tables():
    assert len(get("/api/dashboard2/mainElectionTypes")) == 5
    assert {r["status_name"] for r in get("/api/dashboard2/statuses")} == {
        "Proposed",
        "Confirmed",
    }
    assert len(get("/api/dashboard2/roles")) == 13
    assert len(get("/api/dashboard2/reservations")) == 8
    # Every active election type is claimed by a body; the two picklists must agree.
    bodies = {r["main_election_type_id"] for r in get("/api/dashboard2/mainElectionTypes")}
    for row in get("/api/dashboard2/electionTypes"):
        if row["is_active"] == "Y":
            assert row["main_election_type_id"] in bodies, row["election_type"]
    assemblies = get("/api/dashboard2/assemblies", parliamentId=PARLIAMENT_IDS)
    assert assemblies and all(
        str(a["parliament_id"]) in PARLIAMENT_IDS.split(",") for a in assemblies
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
