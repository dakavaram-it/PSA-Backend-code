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

    main.query = fake_query
    return calls


def context_row(assembly=181, caste=2):
    return {
        "reservation_type": "BC-GENERAL" if caste else None,
        "required_caste_category_id": caste,
        "required_gender": None,
        "assembly_constituency_id": assembly,
    }


def check_search_flags_the_assembly():
    real_query = main.query
    try:
        calls = capture([[context_row()]])
        main.cadre_search(1, "MembershipId", "12345678")
        ctx_sql, ctx_args = calls[0]
        sql, args = calls[1]

        # The proposal constituency's own assembly is what the search is scoped to, so
        # it has to come back with the reservation.
        assert "UA.constituency_id AS assembly_constituency_id" in ctx_sql, ctx_sql
        assert ctx_args == (1,), ctx_args

        # A flag, not a WHERE: the row has to come back so the frontend can say the id
        # belongs to another assembly instead of showing "no cadre found".
        assert (
            "CASE WHEN UA.constituency_id = %s THEN 'Y' ELSE 'N' END AS in_assembly" in sql
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
            [{**context_row(caste=None), "max_proposals": 3}],
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


if __name__ == "__main__":
    check()
    check_search_flags_the_assembly()
    check_assign_refuses_another_assembly()
    print("ok")
