"""Self-check for S22 (the Dashboard's per-assembly position rollup). No DB, no test
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

    main.query = fake_query
    return calls


def check_scoped_by_constituency():
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, args = calls[0]

    assert args == (181,), args
    assert sql.count("%s") == len(args), sql
    assert "WHERE UA.constituency_id = %s AND PCon.enrollment_id = 1" in sql, sql
    # The filter has to sit between the joins and the grouping, or MySQL rejects it.
    assert sql.index("WHERE UA.constituency_id") < sql.index("GROUP BY"), sql


def check_left_join_not_inner():
    # S19 must hide a position nobody was proposed for; S22 must not, since "Not
    # Started" is exactly that position.
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    assert sql.count("proposal_candidate PC") == 1, sql
    assert "LEFT OUTER JOIN proposal_candidate PC" in sql, sql


def check_proposed_status_cnt_requires_explicit_status():
    # The join to proposal_candidate is LEFT, so a position with zero candidates still
    # produces one row with every PC.* column NULL — proposal_status_id included. A
    # missing/unset status must not be defaulted to Proposed here (unlike S19, whose
    # INNER join guarantees a real candidate row): only an explicit 1 counts, so both
    # "no candidate at all" and "a candidate with no status" read as 0, not 1.
    calls = capture([])
    main.get_dashboard_positions_by_constituency_id(181)
    sql, _ = calls[0]
    assert "SUM(CASE WHEN PC.proposal_status_id = 1 THEN 1 ELSE 0 END)" in sql, sql
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
    check_left_join_not_inner()
    check_proposed_status_cnt_requires_explicit_status()
    check_carries_election_type_and_location()
    print("ok")
