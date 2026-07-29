"""Phase 2 of the staged IVRS load: move staging → survey24.ivrs_survey_answer with a
single set-based INSERT … SELECT (one server-side statement; only appends new rows,
so it does not touch the locked existing SID-31 rows).

Usage:
    PYTHONPATH=. python3.13 scripts/merge_ivrs.py [--drop]   # --drop also drops the staging table after
"""
import sys
import time

from sqlalchemy import text

from app.database.db import dakavara_session

STAGE = "dakavara_pa.pulse_trend_ivrs31_stg"
DROP = "--drop" in sys.argv


def main():
    with dakavara_session() as db:
        staged = db.execute(text(f"SELECT COUNT(*) FROM {STAGE}")).scalar()
        before = db.execute(text("SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id=31")).scalar()
        print(f"staging rows={staged:,}  master SID31 before={before:,}", flush=True)
        t = time.time()
        db.execute(text(f"""
            INSERT INTO survey24.ivrs_survey_answer
              (mobile_no, constituency_id, ivrs_survey_id, round_id, clip_no, option_no,
               ivrs_question_id, ivrs_option_id, is_deleted, survey_date)
            SELECT mobile_no, constituency_id, ivrs_survey_id, round_id, clip_no, option_no,
                   ivrs_question_id, ivrs_option_id, 'N', survey_date
            FROM {STAGE}
        """))
        db.commit()
        after = db.execute(text("SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id=31")).scalar()
        print(f"MERGED in {(time.time()-t)/60:.1f} min  master SID31 after={after:,} (+{after-before:,})", flush=True)
        if DROP:
            db.execute(text(f"DROP TABLE {STAGE}")); db.commit(); print("dropped staging table", flush=True)


if __name__ == "__main__":
    main()
