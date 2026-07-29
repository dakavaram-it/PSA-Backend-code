"""One-time panel / loyalty / volatility analysis over the 5 big IVRS rounds
(SIDs 19, 20, 26, 28, 31). Numbers-first; prints as each section completes.

Strategy: materialize the consistent panel (phones in ALL 5) once into a small
staging table we own, then run fast joins off it (loyalty, households, volatile
breakdowns). Reads survey24 via SELECT (not blocked by the write-lock); writes only
our own dakavara_pa staging table.
"""
import time
from sqlalchemy import text
from app.database.db import dakavara_session

PANEL = "19,20,26,28,31"          # the 5 big IVRS rounds
ALLIVRS = "19,20,21,24,25,26,28,31"
TN = "14,28"; P = "14,15,16,28"
STG = "dakavara_pa.pulse_trend_panel5_stg"
BLOC = f"CASE WHEN ivrs_option_id IN ({TN}) THEN 'TN' WHEN ivrs_option_id=15 THEN 'YS' ELSE 'OT' END"
PNAME = {"TN": "TDP/NDA", "YS": "YSRCP", "OT": "Others"}


def q(db, sql):
    return db.execute(text(sql))


def section(title):
    print(f"\n=== {title} ===", flush=True)


def main():
    t0 = time.time()
    with dakavara_session() as db:
        # ── build the panel staging (phones answering the vote-Q in ALL 5 rounds) ──
        section("building panel staging (phones in all 5 rounds)")
        q(db, f"DROP TABLE IF EXISTS {STG}"); db.commit()
        q(db, f"""CREATE TABLE {STG} (
                    mobile_no VARCHAR(15) NOT NULL PRIMARY KEY,
                    nblocs INT, bloc VARCHAR(2)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        q(db, f"""INSERT INTO {STG} (mobile_no, nblocs, bloc)
            SELECT mobile_no, COUNT(DISTINCT bloc) nblocs, MIN(bloc) bloc FROM (
              SELECT mobile_no, ivrs_survey_id, {BLOC} bloc
              FROM survey24.ivrs_survey_answer
              WHERE ivrs_survey_id IN ({PANEL}) AND ivrs_option_id IN ({P})
                AND mobile_no IS NOT NULL AND mobile_no <> ''
            ) x GROUP BY mobile_no HAVING COUNT(DISTINCT ivrs_survey_id) = 5""")
        db.commit()
        print(f"  built in {(time.time()-t0)/60:.1f} min", flush=True)

        # ── 1 + 2: panel size, loyalty, volatility ──
        section("1-2) panel size, loyalty & volatility (across the 5 rounds)")
        panel = q(db, f"SELECT COUNT(*) FROM {STG}").scalar()
        loyal = q(db, f"SELECT COUNT(*) FROM {STG} WHERE nblocs=1").scalar()
        vol = panel - loyal
        print(f"  phones in ALL 5 rounds (panel): {panel:,}", flush=True)
        print(f"  loyal (same party all 5):       {loyal:,} ({100*loyal/panel:.1f}%)", flush=True)
        print(f"  VOLATILE (changed at least once): {vol:,} ({100*vol/panel:.1f}%)", flush=True)
        print("  loyal breakdown by party:", flush=True)
        for r in q(db, f"SELECT bloc, COUNT(*) n FROM {STG} WHERE nblocs=1 GROUP BY bloc ORDER BY n DESC").mappings():
            print(f"     {PNAME.get(r['bloc'], r['bloc']):>8}: {r['n']:,} ({100*r['n']/panel:.1f}% of panel)", flush=True)

        # ── 3: households for the panel ──
        section("3) households (families) for the panel")
        r = q(db, f"""SELECT COUNT(*) matched,
                 COUNT(DISTINCT CONCAT(m.Constituency_name,'|',m.PART_NO,'-',m.HOUSE_NO)) houses
               FROM {STG} s JOIN dakavara_pa.m_main_voter_details m ON m.MOBILE_NO = s.mobile_no
               WHERE m.HOUSE_NO IS NOT NULL AND m.HOUSE_NO <> ''""").mappings().first()
        print(f"  panel phones matched to a household: {r['matched']:,} of {panel:,}", flush=True)
        print(f"  distinct households (families) they belong to: {r['houses']:,}", flush=True)

        # ── 4: volatile-group breakdowns ──
        section("4) volatile group — caste / caste-category / age / gender / latest vote")
        for label, col in [("gender", "im.gender"), ("age", "im.age_range"), ("caste", "im.caste_name")]:
            print(f"  by {label}:", flush=True)
            for r in q(db, f"""SELECT {col} seg, COUNT(*) n
                    FROM {STG} s JOIN survey24.ivrs_mobiles im ON im.mobile_no = s.mobile_no
                    WHERE s.nblocs>1 AND {col} IS NOT NULL AND {col}<>''
                    GROUP BY seg ORDER BY n DESC LIMIT 8""").mappings():
                print(f"     {str(r['seg']):>14}: {r['n']:,} ({100*r['n']/vol:.1f}%)", flush=True)
        print("  volatile voters' LATEST vote (Jun '26 / SID 31):", flush=True)
        for r in q(db, f"""SELECT {BLOC} bloc, COUNT(*) n
                FROM {STG} s JOIN survey24.ivrs_survey_answer a
                  ON a.mobile_no=s.mobile_no AND a.ivrs_survey_id=31 AND a.ivrs_option_id IN ({P})
                WHERE s.nblocs>1 GROUP BY bloc ORDER BY n DESC""").mappings():
            print(f"     {PNAME.get(r['bloc'], r['bloc']):>8}: {r['n']:,}", flush=True)

        # ── coverage: total unique phones + repeat distribution (all IVRS rounds) ──
        section("coverage) unique people & how many repeat (all 8 IVRS rounds)")
        rows = q(db, f"""SELECT nsurv, COUNT(*) phones FROM (
                   SELECT mobile_no, COUNT(DISTINCT ivrs_survey_id) nsurv
                   FROM survey24.ivrs_survey_answer
                   WHERE ivrs_survey_id IN ({ALLIVRS}) AND ivrs_option_id IN ({P})
                     AND mobile_no IS NOT NULL AND mobile_no<>''
                   GROUP BY mobile_no) z GROUP BY nsurv ORDER BY nsurv""").mappings().all()
        total = sum(r["phones"] for r in rows)
        multi = sum(r["phones"] for r in rows if r["nsurv"] >= 2)
        print(f"  total unique people (phones) polled: {total:,}", flush=True)
        print(f"  gave opinion in >=2 rounds (repeat responders): {multi:,} ({100*multi/total:.1f}%)", flush=True)
        for r in rows:
            print(f"     in {r['nsurv']} round(s): {r['phones']:,}", flush=True)

        q(db, f"DROP TABLE IF EXISTS {STG}"); db.commit()
    print(f"\nDONE in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
