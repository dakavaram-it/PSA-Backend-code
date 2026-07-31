"""Self-check for eligibility_flag. No DB, no test framework:

    cd Backend && python test_eligibility.py

Location is no longer part of eligibility: the expression must never mention the
address columns, however the proposal constituency's own address is populated.
Reservation (caste category, gender) must still decide the flag.
"""

import main

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

    print("ok")


if __name__ == "__main__":
    check()
