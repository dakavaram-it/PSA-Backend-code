"""Data access for the Pulse Trend dashboard — AP Assembly (scope 2, MAIN)
election trends + TDP cadre strength. Read-only, dakavara_pa."""
from sqlalchemy import text
from sqlalchemy.orm import Session

YEARS = ("2014", "2019", "2024")  # AP Assembly main elections with clean data


class PulseRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    def party_trend(self):
        # complete_votes_percentage = true state-wide share (votes_percentage is
        # share-among-contested-seats and misleading). MAX dedupes duplicate party rows.
        sql = text("""
            SELECT e.election_year AS yr, p.short_name AS party,
                   MAX(CAST(NULLIF(per.complete_votes_percentage,'') AS DECIMAL(6,2))) AS share,
                   MAX(per.total_votes_gained) AS votes,
                   MAX(CAST(NULLIF(per.total_seats_won,'') AS UNSIGNED)) AS seats
            FROM party_election_result per
            JOIN election e ON e.election_id = per.election_id
            JOIN party p   ON p.party_id   = per.party_id
            WHERE e.election_scope_id = 2 AND e.sub_type = 'MAIN'
              AND e.election_year IN ('2014','2019','2024')
            GROUP BY e.election_year, p.short_name
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]

    def turnout_trend(self):
        sql = text("""
            SELECT e.election_year AS yr,
                   COUNT(*) AS consts,
                   ROUND(AVG(CAST(NULLIF(cer.voting_percentage,'') AS DECIMAL(5,2))),2) AS turnout,
                   SUM(cer.total_votes_polled) AS votesPolled
            FROM constituency_election_result cer
            JOIN constituency_election ce ON ce.consti_elec_id = cer.consti_elec_id
            JOIN election e ON e.election_id = ce.election_id
            WHERE e.election_scope_id = 2 AND e.sub_type = 'MAIN'
              AND e.election_year IN ('2014','2019','2024')
            GROUP BY e.election_year
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]

    # ---- IVRS voting-intention surveys (survey24 schema) ----
    IVRS_IDS = (24, 25, 26, 28)

    def ivrs_vote(self):
        sql = text("""
            SELECT SA.ivrs_survey_id AS sid, O.option_name AS opt, COUNT(SA.mobile_no) AS resp
            FROM survey24.ivrs_survey_answer SA
            JOIN survey24.ivrs_option O ON O.ivrs_option_id = SA.ivrs_option_id
            WHERE SA.ivrs_survey_id IN (24,25,26,28)
              AND O.option_name IN ('TDP','NDA','YSRCP','OTHERS')
            GROUP BY SA.ivrs_survey_id, O.ivrs_option_id
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]

    def ivrs_member_split(self):
        sql = text("""
            SELECT SA.ivrs_survey_id AS sid, O.option_name AS opt,
                   COALESCE(IM.member_type,'Unknown') AS memberType, COUNT(SA.mobile_no) AS resp
            FROM survey24.ivrs_survey_answer SA
            JOIN survey24.ivrs_option O ON O.ivrs_option_id = SA.ivrs_option_id
            LEFT JOIN survey24.ivrs_mobiles IM ON IM.mobile_no = SA.mobile_no
            WHERE SA.ivrs_survey_id IN (24,25,26,28)
              AND O.option_name IN ('TDP','NDA','YSRCP','OTHERS')
            GROUP BY SA.ivrs_survey_id, O.ivrs_option_id, IM.member_type
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]

    def cadre_by_year(self):
        sql = text("""
            SELECT enrollment_year AS yr, COUNT(*) AS members
            FROM tdp_cadre
            WHERE is_deleted = 'N' AND enrollment_year BETWEEN 2009 AND 2026
            GROUP BY enrollment_year ORDER BY enrollment_year
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]
