"""Backend/test_dashboard2.py — the invariants Dashboard 2's numbers rest on.

Runs against the live dakavara_pa database, like ../../portal-frontend-code/Backend's own
tests. It asserts relationships between endpoints rather than fixed figures, so it stays
true as candidates are proposed and confirmed — the only hard numbers here are the position
counts, which are seeded configuration and do not move on their own.

    cd Backend && python test_dashboard2.py       (or: pytest test_dashboard2.py)
"""
import sys
import time

import jwt
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

# ZPTC Member — 671 positions, the post with the most proposed names in the live data, and
# the only one whose locations span every parliament constituency in the state.
ZPTC = dict(proposalRoleId=3)
ZPTC_POSITIONS = 671

# Corporator. The one post whose rows span more than one election type — Municipal Ward,
# Corporation Ward and a stray MPTC constituency — and the reason the API keys on the role
# rather than on the (body, election type, role) triple it used to require.

# The 15 assemblies the caller in the brief scopes to, and two parliaments covering most
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
    assert len(get("/api/dashboard2/roles")) == 14
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


def test_a_post_spanning_election_types_is_one_card():
    """A post is its role id, whatever election types its positions sit under.

    This is the regression the dashboard tree had: keying a post on (body, election type,
    role) split Corporator into a Municipal Ward row and a Corporation Ward row, so the two
    dashboards drew different trees. Asking by role alone has to return their sum.

    The spanning role is taken from the data rather than hardcoded: role 14 Ward Councillor
    was added after this test was written and took the municipal wards off role 5, so
    Corporator no longer spans anything.
    """
    summary = get("/api/dashboard2/positionSummary")["positions"]
    parts_by_role = {}
    for row in summary:
        parts_by_role.setdefault(row["proposal_role_id"], []).append(row)
    role_id, parts = max(parts_by_role.items(), key=lambda kv: len(kv[1]))
    expected = sum(p["total_locations"] for p in parts)

    whole = get("/api/dashboard2/geoBreakdown", proposalRoleId=role_id)
    assert sum(r["total_locations"] for r in whole["parliaments"]) == expected
    assert sum(r["total_locations"] for r in whole["assemblies"]) == expected

    reserved = get("/api/dashboard2/reservationSummary", proposalRoleId=role_id)
    assert reserved["totals"]["total_locations"] == expected

    # And narrowing by election type still carves out exactly one of the parts.
    one = parts[0]
    narrowed = get(
        "/api/dashboard2/reservationSummary",
        proposalRoleId=role_id,
        proposalElectionTypeId=one["proposal_election_type_id"],
    )
    assert narrowed["totals"]["total_locations"] == one["total_locations"]


def test_the_tree_claims_every_position():
    """The fifteen cards' role ids must cover every proposal_role that holds positions.

    A role appearing here that the tree does not claim is a post the dashboards would draw
    in their "Other" group — visible, but outside the agreed layout. Keep this in step with
    Frontend/src/leap/electionTree.js.
    """
    claimed = {4, 6, 7, 3, 12, 13, 8, 9, 5, 14, 10, 11, 1, 2}
    live = {p["proposal_role_id"] for p in get("/api/dashboard2/positionSummary")["positions"]}
    assert live <= claimed, f"unclaimed roles: {sorted(live - claimed)}"


def test_role_id_is_required_and_validated():
    assert client.get("/api/dashboard2/reservationSummary").status_code == 422
    assert (
        client.get("/api/dashboard2/reservationSummary", params={"proposalRoleId": "abc"}).status_code
        == 400
    )


# --- writes ----------------------------------------------------------------
# These hit the live database. Every one of them restores what it changed in a finally, so
# a run leaves proposal_candidate exactly as it found it. If a run is interrupted midway,
# the candidate it borrowed may be left Confirmed — check the id the failure names.


def _token(user_id=1):
    """A portal session token, minted with the same secret /login signs with."""
    from auth import JWT_KEY
    from config import JWT_ALGORITHM

    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + 600}, JWT_KEY, algorithm=JWT_ALGORITHM)


def _auth(user_id=1):
    return {"Authorization": "Bearer " + _token(user_id)}


def _borrow_proposed_candidate():
    """One live Proposed candidate to move around, and its original state."""
    page = get("/api/dashboard2/locations", **ZPTC, stage=1, limit=5)
    for loc in page["locations"]:
        for c in loc["candidates"]:
            if c["proposal_status_id"] == 1 and c["is_nominated"] != "Y":
                return c["proposal_candidate_id"]
    raise AssertionError("no Proposed candidate available to exercise the writes")


def test_writes_reject_anonymous_and_bad_tokens():
    body = {"proposal_candidate_id": 1}
    assert client.post("/api/dashboard2/confirmCandidate", json=body).status_code == 401
    assert client.post("/api/dashboard2/removeCandidate", json=body).status_code == 401
    assert client.post("/api/dashboard2/markNominated", json=body).status_code == 401
    assert client.post(
        "/api/dashboard2/confirmCandidate", json=body, headers={"Authorization": "Bearer nonsense"}
    ).status_code == 401
    # A token signed with the wrong key must not be accepted.
    forged = jwt.encode({"sub": "1", "exp": int(time.time()) + 600}, "wrong-secret", algorithm="HS512")
    assert client.post(
        "/api/dashboard2/confirmCandidate", json=body, headers={"Authorization": "Bearer " + forged}
    ).status_code == 401


def test_unknown_candidate_is_404_not_a_silent_no_op():
    r = client.post("/api/dashboard2/confirmCandidate", json={"proposal_candidate_id": 99999999}, headers=_auth())
    assert r.status_code == 404, r.text


def test_confirm_then_nominate_then_restore():
    """The stage ladder, driven end to end and put back exactly as it was."""
    cid = _borrow_proposed_candidate()
    try:
        r = client.post("/api/dashboard2/confirmCandidate", json={"proposal_candidate_id": cid}, headers=_auth())
        assert r.status_code == 200, r.text
        assert _candidate_state(cid) == (2, "N")

        # Nomination needs a confirmed candidate, and this one now is.
        r = client.post("/api/dashboard2/markNominated", json={"proposal_candidate_id": cid}, headers=_auth())
        assert r.status_code == 200, r.text
        assert _candidate_state(cid) == (2, "Y")

        # A second Confirmed on the same location is refused, not silently allowed.
        sibling = _sibling_candidate(cid)
        if sibling:
            r = client.post(
                "/api/dashboard2/confirmCandidate", json={"proposal_candidate_id": sibling}, headers=_auth()
            )
            assert r.status_code == 409, r.text
    finally:
        client.post(
            "/api/dashboard2/markNominated",
            json={"proposal_candidate_id": cid, "is_nominated": "N"},
            headers=_auth(),
        )
        client.post(
            "/api/dashboard2/confirmCandidate",
            json={"proposal_candidate_id": cid, "proposal_status_id": 1},
            headers=_auth(),
        )
    assert _candidate_state(cid) == (1, "N"), f"failed to restore proposal_candidate {cid}"


def test_nomination_refused_before_confirmation():
    cid = _borrow_proposed_candidate()
    r = client.post("/api/dashboard2/markNominated", json={"proposal_candidate_id": cid}, headers=_auth())
    assert r.status_code == 409, r.text
    assert _candidate_state(cid) == (1, "N"), "a refused write must change nothing"


def _candidate_state(cid):
    from db import run

    row = run(
        "SELECT proposal_status_id, is_nominated FROM proposal_candidate "
        "WHERE proposal_candidate_id = %s AND is_active = 'Y'",
        (cid,),
        one=True,
    )
    return (row["proposal_status_id"], row["is_nominated"])


def _sibling_candidate(cid):
    from db import run

    rows = run(
        "SELECT proposal_candidate_id FROM proposal_candidate WHERE is_active = 'Y' "
        "AND proposal_position_id = (SELECT proposal_position_id FROM proposal_candidate "
        "WHERE proposal_candidate_id = %s) AND proposal_candidate_id <> %s LIMIT 1",
        (cid, cid),
    )
    return rows[0]["proposal_candidate_id"] if rows else None


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
