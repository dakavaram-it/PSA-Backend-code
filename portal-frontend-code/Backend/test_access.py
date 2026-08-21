"""Self-check for the per-user assembly access scoping. No DB, no test framework:

    cd Backend && python test_access.py

The queries are only ever exercised against a live database, where a placeholder that
does not line up with its args reads as an empty picklist rather than an error. These
assert the shapes instead: three user_id placeholders in the union, and getProposalPositionsWithCandidates's IN list
matching the access ids it was built from.
"""

import main


class FakeRequest:
    cookies = {}


def capture(rows):
    """Swap main.query for a recorder, answering every call with `rows`."""
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


def check_union():
    calls = capture([{"constituency_id": 181, "constituency_name": "ACHANTA"}])
    rows = main.user_access_assemblies(3703)
    assert rows[0]["constituency_id"] == 181, rows

    sql, args = calls[0]
    assert args == (3703, 3703, 3703), args
    assert sql.count("%s") == len(args), sql
    # One SELECT per grant: state, parliament, assembly.
    assert sql.count("UNION") == 2, sql
    assert "user_state_access_info" in sql and "user_constituency_access_info" in sql, sql
    assert "ES.election_type_id = 2" in sql, sql
    assert "C.deform_date IS NULL" in sql, sql


def check_s19_scoped():
    calls = capture([])
    main.user_access_assemblies = lambda user_id: [
        {"constituency_id": 181, "constituency_name": "ACHANTA"},
        {"constituency_id": 182, "constituency_name": "PALAKOL"},
    ]
    main.current_user = lambda request: {"user_id": 3703}

    main.get_proposal_positions_with_candidates(FakeRequest())
    sql, args = calls[0]
    assert "WHERE AC.constituency_id IN (%s, %s)" in sql, sql
    # Two status ids in the SELECT before the access list: the COALESCE default and the
    # value each of the two per-status counts compares against.
    assert args == (
        main.PROPOSED_STATUS_ID,
        main.PROPOSED_STATUS_ID,
        main.CONFIRMED_STATUS_ID,
        181,
        182,
    ), args
    assert sql.count("%s") == len(args), sql
    # The filter has to sit between the joins and the grouping, or MySQL rejects it.
    assert sql.index("WHERE AC.constituency_id") < sql.index("GROUP BY"), sql


def check_s19_no_access():
    calls = capture([])
    main.user_access_assemblies = lambda user_id: []
    main.current_user = lambda request: {"user_id": 9999}

    # No grants means no positions — never every position.
    assert main.get_proposal_positions_with_candidates(FakeRequest()) == []
    assert calls == [], calls


if __name__ == "__main__":
    check_union()
    check_s19_scoped()
    check_s19_no_access()
    print("ok")
