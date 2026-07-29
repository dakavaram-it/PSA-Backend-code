"""Materialize a per-voter matrix for the dynamic Panel & Volatility view.

One row per phone that answered the vote question in any IVRS round, with:
  - bloc code per survey (s19,s20,s21,s24,s25,s26,s28,s31): 1=TDP/NDA, 2=YSRCP, 3=Others, NULL=absent
  - caste_name, caste_category, age_range, gender   (from ivrs_mobiles)
  - hh = household key 'constituency|part-house'      (from m_main roll, --houses step)

This table is loaded into FastAPI memory; any survey subset is then computed in <1s.
Reads survey24 via SELECT; writes only our dakavara_pa table.

Usage:
    PYTHONPATH=. python3.13 scripts/materialize_voter_matrix.py            # blocs + attributes
    PYTHONPATH=. python3.13 scripts/materialize_voter_matrix.py --houses    # also fill hh (heavy)
    PYTHONPATH=. python3.13 scripts/materialize_voter_matrix.py --houses-only
"""
import sys
import time
from sqlalchemy import text
from app.database.db import dakavara_session

T = "dakavara_pa.pulse_trend_voter_matrix"
SIDS = [19, 20, 21, 24, 25, 26, 28, 31]
P = "14,15,16,28"
CODE = "CASE WHEN ivrs_option_id IN (14,28) THEN 1 WHEN ivrs_option_id=15 THEN 2 ELSE 3 END"
HOUSES = "--houses" in sys.argv or "--houses-only" in sys.argv
HOUSES_ONLY = "--houses-only" in sys.argv


def main():
    t0 = time.time()
    with dakavara_session() as db:
        if not HOUSES_ONLY:
            print("[matrix] creating table…", flush=True)
            db.execute(text(f"DROP TABLE IF EXISTS {T}")); db.commit()
            cols = ", ".join(f"s{n} TINYINT" for n in SIDS)
            db.execute(text(f"""CREATE TABLE {T} (
                mobile_no VARCHAR(15) NOT NULL PRIMARY KEY, {cols},
                caste_name VARCHAR(80), caste_category VARCHAR(40),
                age_range VARCHAR(20), gender VARCHAR(4), hh VARCHAR(96)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""))
            db.commit()

            print("[matrix] pivot insert (bloc per survey)…", flush=True)
            t = time.time()
            sel = ", ".join(f"MAX(CASE WHEN sid={n} THEN code END) s{n}" for n in SIDS)
            db.execute(text(f"""
                INSERT INTO {T} (mobile_no, {", ".join(f"s{n}" for n in SIDS)})
                SELECT mobile_no, {sel} FROM (
                  SELECT mobile_no, ivrs_survey_id sid, {CODE} code
                  FROM survey24.ivrs_survey_answer
                  WHERE ivrs_survey_id IN ({",".join(map(str, SIDS))}) AND ivrs_option_id IN ({P})
                    AND mobile_no IS NOT NULL AND mobile_no <> ''
                ) x GROUP BY mobile_no"""))
            db.commit()
            n = db.execute(text(f"SELECT COUNT(*) FROM {T}")).scalar()
            print(f"[matrix] {n:,} voters in {(time.time()-t)/60:.1f} min", flush=True)

            print("[matrix] update attributes (caste/category/age/gender)…", flush=True)
            t = time.time()
            db.execute(text(f"""
                UPDATE {T} mtx
                JOIN survey24.ivrs_mobiles im ON im.mobile_no = mtx.mobile_no
                LEFT JOIN dakavara_pa.caste_state cs ON cs.caste_state_id = im.caste_state_id
                LEFT JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id = cs.caste_category_group_id
                SET mtx.caste_name = im.caste_name, mtx.age_range = im.age_range,
                    mtx.gender = im.gender, mtx.caste_category = cg.caste_category_group_name"""))
            db.commit()
            print(f"[matrix] attributes set in {(time.time()-t)/60:.1f} min", flush=True)

        if HOUSES:
            print("[matrix] update households from voter roll (heavy)…", flush=True)
            t = time.time()
            db.execute(text(f"""
                UPDATE {T} mtx
                JOIN dakavara_pa.m_main_voter_details mm ON mm.MOBILE_NO = mtx.mobile_no
                SET mtx.hh = CONCAT(mm.Constituency_name,'|',mm.PART_NO,'-',mm.HOUSE_NO)
                WHERE mm.HOUSE_NO IS NOT NULL AND mm.HOUSE_NO <> ''"""))
            db.commit()
            hh = db.execute(text(f"SELECT COUNT(*) FROM {T} WHERE hh IS NOT NULL")).scalar()
            print(f"[matrix] {hh:,} voters got a household in {(time.time()-t)/60:.1f} min", flush=True)

    print(f"[matrix] DONE in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
