"""Self-check for getDashboardPositionsByConstituencyId (the Dashboard's per-assembly position rollup). No DB, no test
framework:

    cd Backend && python test_dashboard.py

Mirrors test_access.py's approach: swap main.query for a recorder and assert on the SQL
shape rather than on live data, since the query only ever runs against production.
"""

import main


def capture(rows):
    calls = []

    def fake_query(sql, args=None):
        calls.append((sql, args))
        return rows

    # pp_active() probes the schema through main.query on first use, which would land in
    # `calls` as a phantom first query and shift every index below. Pinning it to "" is
    # also what these assertions describe: the SQL without the proposal_position.is_active
    # filter, which is what the column's absence produces.
    main._PP_ACTIVE = ""
    main.query = fake_query
    return calls


def check_scoped_by_constituency():
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, args = calls[0]

    # Three branches, one assembly id each: the address's own constituency_id, the
    # whole-body rows that self-reference and resolve through
    # assembly_local_election_body, and the district-level rows that have neither.
    # The two per-status counts bind their ids in the SELECT, before the three assembly
    # branches in the WHERE.
    assert args == (
        main.PROPOSED_STATUS_ID,
        main.CONFIRMED_STATUS_ID,
        181,
        181,
        181,
    ), args
    assert sql.count("%s") == len(args), sql
    assert "WHERE PCon.enrollment_id = 1 AND (UA.constituency_id = %s" in sql, sql
    assert "assembly_local_election_body AL" in sql, sql
    assert "UA.constituency_id IS NULL" in sql, sql
    # The filter has to sit between the joins and the grouping, or MySQL rejects it.
    assert sql.index("WHERE PCon.enrollment_id") < sql.index("GROUP BY"), sql


def check_whole_body_and_district_rows_are_named():
    # A Municipality/Corporation row has no tehsil and a Zilla Parishath row has neither
    # tehsil nor town — without the district branch the location column came back NULL.
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    assert "CONCAT(L.name, ' Town')" in sql, sql
    assert "CONCAT(D.district_name, ' District')" in sql, sql
    assert "LEFT OUTER JOIN district D ON UA.district_id = D.district_id" in sql, sql
    # Every non-aggregated CASE input has to be grouped by, or ONLY_FULL_GROUP_BY rejects it.
    grouping = sql[sql.index("GROUP BY"):]
    assert "D.district_name" in grouping, grouping


def check_status_drilldown_matches_the_tiles():
    # The tile and the list it opens must be scoped the same way, or a body the tiles
    # count opens an empty drill-down.
    calls = capture([])
    main.get_dashboard_candidates_by_status(181, 3, 1)
    sql, args = calls[0]
    assert args == (181, 181, 181, 3, 1), args
    assert sql.count("%s") == len(args), sql
    assert main.assembly_match() in sql, sql


def check_left_join_not_inner():
    # getProposalPositionsWithCandidates must hide a position nobody was proposed for; getDashboardPositionsByConstituencyId must not, since "Not
    # Started" is exactly that position.
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    assert sql.count("proposal_candidate PC") == 1, sql
    assert "LEFT OUTER JOIN proposal_candidate PC" in sql, sql


def check_proposed_status_cnt_requires_explicit_status():
    # The join to proposal_candidate is LEFT, so a position with zero candidates still
    # produces one row with every PC.* column NULL — proposal_status_id included. A
    # missing/unset status must not be defaulted to Proposed here (unlike getProposalPositionsWithCandidates, whose
    # INNER join guarantees a real candidate row): only an explicit 1 counts, so both
    # "no candidate at all" and "a candidate with no status" read as 0, not 1.
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    assert "SUM(CASE WHEN PC.proposal_status_id = %s THEN 1 ELSE 0 END)" in sql, sql
    assert "COALESCE(PC.proposal_status_id" not in sql, sql


def check_carries_election_type_and_location():
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    for column in (
        "PET.proposal_election_type_id",
        "PET.election_type",
        "PCon.proposal_consituency_id AS proposal_constituency_id",
        "LB.name AS local_body_name",
        "proposed_status_cnt",
        "conformed_status_cnt",
        "T.tehsil_id",
        "L.local_election_body_id AS town_id",
    ):
        assert column in sql, f"{column} missing: {sql}"


if __name__ == "__main__":
    check_scoped_by_constituency()
    check_whole_body_and_district_rows_are_named()
    check_status_drilldown_matches_the_tiles()
    check_left_join_not_inner()
    check_proposed_status_cnt_requires_explicit_status()
    check_carries_election_type_and_location()
    print("ok")
