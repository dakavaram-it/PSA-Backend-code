"""Self-check for eligibility_flag and the assembly scope. No DB, no test framework:

    cd Backend && python test_eligibility.py

eligibility_flag is the reservation alone (caste category, gender) — it must never mention
the address columns. The assembly is carried separately, as cadre_search's own
`in_assembly` flag and a re-check in assign_proposal_candidate, so a cadre from another
assembly is named as such by the search and cannot be assigned even if one is asked for
directly.
"""

import main
from fastapi import HTTPException

ADDRESS_COLUMNS = ("constituency_id", "tehsil_id", "panchayat_id", "local_election_body")


def ctx(caste=None, gender=None):
    # The address keys are the ones the old filter keyed off; they stay in the dict
    # shape a caller might hand over, and must simply be ignored now.
    return {
        "constituency_id": 181,
        "tehsil_id": 658,
        "panchayat_id": 58153,
        "local_election_body": None,
        "reservation_type": "BC-GENERAL" if caste else None,
        "required_caste_category_id": caste,
        "required_gender": gender,
    }


def check():
    # No reservation: everyone the search matches is eligible.
    sql, args = main.eligibility_flag(ctx())
    assert sql == "'Y' AS eligible", sql
    assert args == [], args

    sql, args = main.eligibility_flag(ctx(caste=2))
    assert sql == "CASE WHEN CCG.caste_category_id = %s THEN 'Y' ELSE 'N' END AS eligible", sql
    assert args == [2], args

    sql, args = main.eligibility_flag(ctx(caste=2, gender="F"))
    assert sql == (
        "CASE WHEN CCG.caste_category_id = %s AND TC.gender = 'F' "
        "THEN 'Y' ELSE 'N' END AS eligible"
    ), sql
    assert args == [2], args

    sql, args = main.eligibility_flag(ctx(gender="F"))
    assert sql == "CASE WHEN TC.gender = 'F' THEN 'Y' ELSE 'N' END AS eligible", sql
    assert args == [], args

    for c, g in ((None, None), (2, None), (2, "F"), (None, "F")):
        sql, args = main.eligibility_flag(ctx(caste=c, gender=g))
        for column in ADDRESS_COLUMNS:
            assert column not in sql, f"{column} still filtered: {sql}"
        # It goes straight after SELECT, so it always names the column the frontend
        # reads, and its placeholders always match the args returned with it.
        assert sql.endswith(" AS eligible"), sql
        assert sql.count("%s") == len(args), sql


def capture(responses):
    """Swap main.query for a recorder answering each call from `responses` in order."""
    calls = []

    def fake_query(sql, args=None):
        calls.append((sql, args))
        return responses[len(calls) - 1] if len(calls) <= len(responses) else []

    # pp_active() probes the schema through main.query on first use, which would land in
    # `calls` as a phantom first query and shift every index below. Pinning it to "" is
    # also what these assertions describe: the SQL without the proposal_position.is_active
    # filter, which is what the column's absence produces.
    main._PP_ACTIVE = ""
    main.query = fake_query
    return calls


def context_row(assembly=181, caste=2, body=58153, local_election_body=None, district=None):
    """One proposal_consituency's address row, in the shape proposal_context selects it.
    The default is a mandal-level body: user_address.constituency_id IS the assembly."""
    return {
        "reservation_type": "BC-GENERAL" if caste else None,
        "required_caste_category_id": caste,
        "required_gender": None,
        "ua_constituency_id": assembly,
        "body_constituency_id": body,
        "local_election_body": local_election_body,
        "district_id": district,
    }


def check_assembly_resolution():
    """The three address shapes assembly_match() knows about, resolved back to assemblies.
    Getting this wrong is what made every Municipality / Corporation / Zilla Parishath
    search answer "belongs to another assembly" for every cadre."""
    real_query = main.query
    try:
        # Mandal / ward: user_address.constituency_id is the assembly itself. No lookup.
        calls = capture([])
        assert main.assembly_constituency_ids(context_row()) == [181]
        assert calls == [], calls

        # Municipality / Corporation: the address points at the body's own constituency,
        # and the assemblies are only reachable through assembly_local_election_body.
        calls = capture([[{"constituency_id": 21}, {"constituency_id": 22}]])
        row = context_row(assembly=900, body=900, local_election_body=77)
        assert main.assembly_constituency_ids(row) == [21, 22]
        assert "assembly_local_election_body" in calls[0][0], calls[0][0]
        assert calls[0][1] == (77,), calls[0][1]

        # Zilla Parishath: no constituency and no local body — a ZP is a district, and
        # every assembly in it counts.
        calls = capture([[{"constituency_id": 31}]])
        row = context_row(assembly=None, body=900, district=11)
        assert main.assembly_constituency_ids(row) == [31]
        assert "district_id = %s" in calls[0][0], calls[0][0]
        assert calls[0][1] == (11,), calls[0][1]
    finally:
        main.query = real_query


def check_search_flags_the_assembly():
    real_query = main.query
    try:
        calls = capture([[context_row()]])
        main.cadre_search(1, "MembershipId", "12345678")
        ctx_sql, ctx_args = calls[0]
        sql, args = calls[1]

        # The proposal constituency's own address is what the search is scoped to, so it
        # has to come back with the reservation.
        assert "UA.constituency_id AS ua_constituency_id" in ctx_sql, ctx_sql
        # The reservation is the position's own column, not the local body's — every
        # proposal_consituency row in the database leaves that one NULL.
        assert "ON PP.constituency_reservation_id = CR.constituency_reservation_id" in ctx_sql, ctx_sql
        assert "WHERE PP.proposal_position_id = %s" in ctx_sql, ctx_sql
        assert ctx_args == (1,), ctx_args

        # A flag, not a WHERE: the row has to come back so the frontend can say the id
        # belongs to another assembly instead of showing "no cadre found".
        assert (
            "CASE WHEN UA.constituency_id IN (%s) THEN 'Y' ELSE 'N' END AS in_assembly" in sql
        ), sql
        assert sql.index("AS in_assembly") < sql.index("WHERE"), sql
        assert "UA.constituency_id = %s AND" not in sql, sql
        # SELECT expression args first (reservation, then assembly), then the search value.
        assert args == (2, 181, "12345678"), args
        assert sql.count("%s") == len(args), sql
    finally:
        main.query = real_query


def check_assign_refuses_another_assembly():
    real_query = main.query
    try:
        capture([
            [context_row(caste=None)],
            [{"proposal_status_id": 1}],
            [{"gender": "M", "caste_category_id": 2, "assembly_constituency_id": 999}],
        ])
        body = main.AssignProposalCandidate(proposal_position_id=1, tdp_cadre_id=2)
        try:
            main.assign_proposal_candidate(body, None)
        except HTTPException as err:
            assert err.status_code == 409, err.status_code
            assert "assembly" in err.detail, err.detail
        else:
            raise AssertionError("assign accepted a cadre from another assembly")
    finally:
        main.query = real_query


def check_assign_refuses_a_completed_position():
    """One Confirmed candidate completes the position: nobody else may be proposed for it,
    however many max_proposals slots are still unused. Separate from the full-position
    refusal because they are different situations — a full position reopens when somebody
    is removed, a completed one does not."""
    real_query = main.query
    try:
        calls = capture([
            [context_row(caste=None)],
            [{"proposal_status_id": 1}],
            [{"gender": "M", "caste_category_id": 2, "assembly_constituency_id": 181}],
            [{"availability": "Completed"}],
        ])
        body = main.AssignProposalCandidate(proposal_position_id=1, tdp_cadre_id=2)
        try:
            main.assign_proposal_candidate(body, None)
        except HTTPException as err:
            assert err.status_code == 409, err.status_code
            assert "completed" in err.detail, err.detail
        else:
            raise AssertionError("assign accepted a candidate for a completed position")

        # The availability query is what decides it, and it is one query: Completed beats
        # the slot count, so a position with free slots and a confirmed candidate is shut.
        sql, args = calls[3]
        assert "THEN 'Completed'" in sql, sql
        assert sql.index("THEN 'Completed'") < sql.index("THEN 'Available'"), sql
        assert args[0] == main.CONFIRMED_STATUS_ID, args
        assert sql.count("%s") == len(args), sql
    finally:
        main.query = real_query


def check_name_search_is_scoped():
    """A name is a substring match, so it is bounded where the exact filters are not:
    current enrollment year, the position's own assemblies, and a row cap."""
    real_query = main.query
    try:
        calls = capture([[context_row()], [{"tdp_cadre_id": 7}, {"tdp_cadre_id": 9}]])
        main.cadre_search(1, "CadreName", "Kamal")

        # Step one picks the ids: enrollment year, the position's assemblies, the LIKE,
        # and a row cap — and none of the card's columns, which is what keeps it off
        # user_address as the driving table.
        ids_sql, ids_args = calls[1]
        assert "JOIN tdp_cadre_enrollment_year EY" in ids_sql, ids_sql
        assert "EY.is_deleted = 'N' AND EY.enrollment_year_id = %s" in ids_sql, ids_sql
        assert "AND UA.constituency_id IN (%s)" in ids_sql, ids_sql
        assert ids_sql.endswith(f" LIMIT {main.NAME_SEARCH_LIMIT}"), ids_sql
        assert ids_args == (main.ENROLLMENT_YEAR_ID, 181, "%Kamal%"), ids_args
        assert ids_sql.count("%s") == len(ids_args), ids_sql

        # Step two decorates exactly those ids — no name, no LIMIT, no year join.
        sql, args = calls[2]
        assert "TC.tdp_cadre_id IN (%s, %s)" in sql, sql
        assert "tdp_cadre_enrollment_year" not in sql, sql
        # Reservation, then the in_assembly flag, then the ids.
        assert args == (2, 181, 7, 9), args
        assert sql.count("%s") == len(args), sql

        # No match: the decorating query is never run at all.
        calls = capture([[context_row()], []])
        assert main.cadre_search(1, "CadreName", "Kamal") == []
        assert len(calls) == 2, calls

        # An exact filter keeps its state-wide reach and its flag-don't-filter contract,
        # in one query.
        calls = capture([[context_row()]])
        main.cadre_search(1, "MembershipId", "12345678")
        assert len(calls) == 2, calls
        sql, _ = calls[1]
        assert "tdp_cadre_enrollment_year" not in sql, sql
        assert "LIMIT" not in sql, sql
    finally:
        main.query = real_query


if __name__ == "__main__":
    check()
    check_assembly_resolution()
    check_search_flags_the_assembly()
    check_name_search_is_scoped()
    check_assign_refuses_another_assembly()
    check_assign_refuses_a_completed_position()
    print("ok")
