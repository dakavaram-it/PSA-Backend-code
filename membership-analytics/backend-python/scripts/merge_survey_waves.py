"""Phase 2 of the survey load: register ivrs_survey rows and merge the staged vendor
waves (dakavara_pa.survey_stg_*) into survey24.ivrs_survey_answer.

Append-only, idempotent per sid: re-running DELETEs the sid first, so it never duplicates.
New sids 40–45 are NOT in pulse_config's wave→sid lists, so this is additive and does not
change the live dashboard until that config is updated. Fully reversible:
    DELETE FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id BETWEEN 40 AND 45;
    DELETE FROM survey24.ivrs_survey        WHERE ivrs_survey_id BETWEEN 40 AND 45;

Usage:
    PYTHONPATH=. python3.13 scripts/merge_survey_waves.py            # DRY-RUN (counts only)
    PYTHONPATH=. python3.13 scripts/merge_survey_waves.py --commit   # register + merge
    PYTHONPATH=. python3.13 scripts/merge_survey_waves.py --commit --only 45
"""
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings

# Dedicated engine with a LONG read/write timeout — the big INSERT…SELECT (6.7M rows)
# runs well past the app's default 600s read_timeout, which caused a 2013 disconnect.
_eng = create_engine(
    get_settings().dakavara_url, pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 3600, "write_timeout": 3600},
)

# sid -> (staging table, ivrs_survey name, survey_date, survey_type_id)  [CATI=2, IVRS=3]
WAVES = {
    40: ("survey_stg_codemo_40", "CODEMO CATI JUN2025", "2025-06-01", 2),
    41: ("survey_stg_codemo_41", "CODEMO CATI NOV2025", "2025-11-01", 2),
    42: ("survey_stg_codemo_42", "CODEMO CATI JUN2026", "2026-06-01", 2),
    43: ("survey_stg_dhruva_43", "DHRUVA IVRS JUN2025", "2025-06-01", 3),
    44: ("survey_stg_dhruva_44", "DHRUVA IVRS NOV2025", "2025-11-01", 3),
    45: ("survey_stg_dhruva_45", "DHRUVA IVRS JUN2026", "2026-05-05", 3),
    46: ("survey_stg_csds_46", "CSDS CAPI JUN2026", "2026-05-04", 1),  # CAPI = face-to-face
}


def scalar(sql, **p):
    with _eng.connect() as c:
        return c.execute(text(sql), p).scalar()


def run1(sql, what, **p):
    """Run one statement on a FRESH connection + commit; retry on lock (1205/1213) or
    lost-connection (2013). Fresh connection per call so a drop can't cascade."""
    waits = 0
    while True:
        try:
            with _eng.connect() as c:
                c.execute(text(sql), p); c.commit()
            return
        except OperationalError as e:
            code = e.orig.args[0] if e.orig and e.orig.args else None
            if code in (1205, 1213, 2013):
                waits += 1
                if waits % 4 == 1:
                    print(f"  [{what}] busy/dropped ({code}), retrying…", flush=True)
                time.sleep(5); continue
            raise


def main():
    commit = "--commit" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = int(sys.argv[sys.argv.index("--only") + 1])
    sids = [only] if only else list(WAVES)
    print(f"mode={'COMMIT' if commit else 'DRY-RUN'}  sids={sids}\n", flush=True)

    grand = 0
    for sid in sids:
        stg, name, sdate, stype = WAVES[sid]
        staged = scalar(f"SELECT COUNT(*) FROM dakavara_pa.{stg}")
        grand += staged
        existing = scalar("SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id=:s", s=sid)
        print(f"sid {sid}  {name:20} staged={staged:>9,}  master_now={existing:,}", flush=True)
        if not commit:
            continue

        # 1) register/refresh the ivrs_survey row
        run1("""INSERT INTO survey24.ivrs_survey (ivrs_survey_id, survey_name, survey_date, survey_type_id)
                VALUES (:id, :nm, :dt, :ty)
                ON DUPLICATE KEY UPDATE survey_name=VALUES(survey_name),
                    survey_date=VALUES(survey_date), survey_type_id=VALUES(survey_type_id)""",
             f"sid{sid}-survey", id=sid, nm=name, dt=sdate, ty=stype)

        # 2) idempotent: clear any prior rows for this sid, then INSERT…SELECT from staging
        if existing:
            print(f"  clearing {existing:,} existing rows for sid={sid}…", flush=True)
            run1("DELETE FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id=:s", f"sid{sid}-del", s=sid)
        t = time.time()
        run1(f"""INSERT INTO survey24.ivrs_survey_answer
                  (mobile_no, constituency_id, ivrs_survey_id, round_id, clip_no, option_no,
                   ivrs_question_id, ivrs_option_id, is_deleted, survey_date)
                SELECT mobile_no, constituency_id, ivrs_survey_id, round_id, clip_no, option_no,
                       ivrs_question_id, ivrs_option_id, 'N', survey_date
                FROM dakavara_pa.{stg}""", f"sid{sid}-merge")
        after = scalar("SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id=:s", s=sid)
        print(f"  MERGED sid {sid}: {after:,} rows in {(time.time()-t)/60:.1f} min", flush=True)

    print(f"\n{'would merge' if not commit else 'merged'} {grand:,} rows total across {len(sids)} waves.", flush=True)


if __name__ == "__main__":
    main()
