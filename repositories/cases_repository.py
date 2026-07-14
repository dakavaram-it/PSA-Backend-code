"""Data access for the Cases (membership-analytics) dashboard.
Reads the cases_* tables loaded from the Cases Register, joined to tdp_cadre."""
from sqlalchemy import text
from sqlalchemy.orm import Session

# sections considered "serious" (attempt-to-murder, murder, rape, grievous hurt,
# dacoity/robbery, culpable homicide) + any SC/ST atrocity charge.
SERIOUS_SECTIONS = ('302', '307', '308', '304', '326', '354', '376', '395', '396', '397')


class CasesRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    # ---- geography for dropdowns ----
    def geo_tree(self):
        sql = text("""
            SELECT parliament, constituency,
                   COUNT(*) AS cases
            FROM cases_fir
            WHERE parliament IS NOT NULL AND parliament <> ''
            GROUP BY parliament, constituency
            ORDER BY parliament, constituency
        """)
        return [dict(r) for r in self.db.execute(sql).mappings().all()]

    # ---- area overview (charts) ----
    def overview(self, parliament=None, constituency=None):
        where, params = self._geo_where(parliament, constituency)
        base = f"""FROM cases_accused a JOIN cases_fir f ON f.case_id=a.case_id {where}"""
        agg = self.db.execute(text(f"""
            SELECT COUNT(*) AS totalAccused,
                   COUNT(DISTINCT a.case_id) AS totalCases,
                   SUM(f.new_status='PT') AS pendingTrial,
                   SUM(f.new_status='D')  AS disposed,
                   SUM(f.new_status='UI') AS underInvestigation,
                   SUM(f.c_nc='NC')       AS nonCompoundable,
                   SUM(a.tdp_cadre_id IS NOT NULL) AS linkedMembers
            {base}"""), params).mappings().first()
        party = self.db.execute(text(f"""
            SELECT COALESCE(a.party_affiliation,'Unknown') AS label, COUNT(*) AS value
            {base} GROUP BY label ORDER BY value DESC LIMIT 10"""), params).mappings().all()
        heads = self.db.execute(text(f"""
            SELECT COALESCE(NULLIF(f.crime_head,''),'Unknown') AS label, COUNT(*) AS value
            {base} GROUP BY label ORDER BY value DESC LIMIT 40"""), params).mappings().all()
        return {"summary": dict(agg or {}),
                "byParty": [dict(r) for r in party],
                "byCrimeHead": [dict(r) for r in heads]}

    # ---- shared leader-aggregation query ----
    def _leader_query(self, where, params, order="totalCases DESC, seriousCases DESC",
                      extra_cte="", having=""):
        serious_in = ",".join(f"'{x}'" for x in SERIOUS_SECTIONS)
        sql = text(f"""
            WITH serious AS (
                SELECT DISTINCT case_id FROM cases_section
                WHERE section_no IN ({serious_in}) OR act='SC/ST POA Act'
            ){extra_cte}
            SELECT
              CASE WHEN a.tdp_cadre_id IS NOT NULL THEN CONCAT('c:',a.tdp_cadre_id)
                   ELSE CONCAT('n:',LOWER(a.accused_name)) END AS leaderKey,
              MAX(a.tdp_cadre_id) AS tdpCadreId,
              COALESCE(MAX(NULLIF(TRIM(CONCAT(COALESCE(cr.first_name,''),' ',COALESCE(cr.last_name,''))),'')),
                       MAX(a.accused_name)) AS name,
              MAX(a.current_designation) AS designation,
              MAX(cr.designation_name)   AS cadreDesignation,
              MAX(a.party_affiliation)   AS party,
              MAX(cr.mobile_no)          AS mobile,
              MAX(cr.image)              AS photo,
              MAX(a.matched_mid)         AS matchedMid,
              MAX(f.parliament)          AS parliament,
              MAX(f.constituency)        AS constituency,
              COUNT(DISTINCT f.case_id)                                   AS totalCases,
              COUNT(DISTINCT IF(f.new_status='PT', f.case_id, NULL))       AS ptCases,
              COUNT(DISTINCT IF(f.new_status='D',  f.case_id, NULL))       AS disposedCases,
              COUNT(DISTINCT IF(f.new_status='UI', f.case_id, NULL))       AS uiCases,
              COUNT(DISTINCT IF(f.c_nc='NC',       f.case_id, NULL))       AS ncCases,
              COUNT(DISTINCT s.case_id)                                    AS seriousCases,
              COUNT(DISTINCT f.police_station)                            AS policeStations
            FROM cases_accused a
            JOIN cases_fir f ON f.case_id=a.case_id
            LEFT JOIN serious s ON s.case_id=a.case_id
            LEFT JOIN tdp_cadre cr ON cr.tdp_cadre_id=a.tdp_cadre_id
            {where}
            GROUP BY leaderKey
            {having}
            ORDER BY {order}
            LIMIT :limit
        """)
        return [dict(r) for r in self.db.execute(sql, params).mappings().all()]

    # ---- leaders list ----
    def leaders(self, parliament=None, constituency=None, scope='leaders', limit=200):
        where, params = self._geo_where(parliament, constituency)
        params['limit'] = limit
        if scope == 'leaders':
            where += (" AND " if where else "WHERE ") + (
                "(a.tdp_cadre_id IS NOT NULL OR (a.current_designation IS NOT NULL "
                "AND a.current_designation NOT IN ('No Designation','')))")
        return self._leader_query(where, params)

    # ---- search by MID or name ----
    # Match leader KEYS first, then aggregate ALL their rows — otherwise a
    # multi-case leader whose other rows lack the search term gets undercounted.
    def search(self, q, limit=50):
        q = (q or "").strip()
        digits = "".join(ch for ch in q if ch.isdigit())
        params = {"like": f"%{q.lower()}%", "limit": limit}
        clauses = [
            "LOWER(a.accused_name) LIKE :like",
            "LOWER(TRIM(CONCAT(COALESCE(cr.first_name,''),' ',COALESCE(cr.last_name,'')))) LIKE :like",
        ]
        if digits:
            params["mid"] = digits
            clauses.append("cr.membership_id = :mid")
            clauses.append("REPLACE(REPLACE(a.matched_mid,'#',''),' ','') = :mid")
        extra_cte = f"""
            , matched_keys AS (
                SELECT DISTINCT
                  CASE WHEN a.tdp_cadre_id IS NOT NULL THEN CONCAT('c:',a.tdp_cadre_id)
                       ELSE CONCAT('n:',LOWER(a.accused_name)) END AS lk
                FROM cases_accused a
                LEFT JOIN tdp_cadre cr ON cr.tdp_cadre_id=a.tdp_cadre_id
                WHERE ({" OR ".join(clauses)})
            )"""
        return self._leader_query("", params, extra_cte=extra_cte,
                                  having="HAVING leaderKey IN (SELECT lk FROM matched_keys)")

    # ---- single leader detail ----
    def leader_cases(self, leader_key):
        if leader_key.startswith('c:'):
            cond, val = "a.tdp_cadre_id = :v", int(leader_key[2:])
        else:
            cond, val = "LOWER(a.accused_name) = :v", leader_key[2:]
        serious_in = ",".join(f"'{x}'" for x in SERIOUS_SECTIONS)
        sql = text(f"""
            SELECT f.case_id AS caseId, f.fir_no AS firNo, f.police_station AS policeStation,
                   f.parliament, f.constituency, f.district,
                   f.crime_head AS crimeHead, f.new_status AS status, f.c_nc AS cnc,
                   f.case_status AS caseStatus, f.court_name AS court, f.section AS sectionRaw,
                   MAX(a.party_affiliation) AS party, MAX(a.current_designation) AS designation,
                   EXISTS(SELECT 1 FROM cases_section cs WHERE cs.case_id=f.case_id
                          AND (cs.section_no IN ({serious_in}) OR cs.act='SC/ST POA Act')) AS isSerious,
                   (SELECT GROUP_CONCAT(DISTINCT CONCAT(cs.section_no,
                           IF(cs.act IS NULL,'',CONCAT(' ',cs.act))) ORDER BY cs.seq SEPARATOR ', ')
                    FROM cases_section cs WHERE cs.case_id=f.case_id) AS sections
            FROM cases_accused a JOIN cases_fir f ON f.case_id=a.case_id
            WHERE {cond}
            GROUP BY f.case_id
            ORDER BY f.new_status, f.fir_no
        """)
        return [dict(r) for r in self.db.execute(sql, {"v": val}).mappings().all()]

    def cadre_meta(self, tdp_cadre_id):
        if not tdp_cadre_id:
            return None
        sql = text("""
            SELECT cr.tdp_cadre_id AS tdpCadreId, CONCAT('#',cr.membership_id) AS mid,
                   TRIM(CONCAT(COALESCE(cr.first_name,''),' ',COALESCE(cr.last_name,''))) AS name,
                   cr.mobile_no AS mobile, cr.age, cr.gender,
                   cr.designation_name AS designation, cr.image AS photo,
                   AC.name AS assembly, PC.name AS parliament
            FROM tdp_cadre cr
            LEFT JOIN constituency CONS ON cr.constituency_id=CONS.constituency_id
            LEFT JOIN constituency AC ON CONS.assembly_constituency_id=AC.constituency_id
            LEFT JOIN constituency PC ON CONS.parliament_id=PC.constituency_id
            WHERE cr.tdp_cadre_id=:id LIMIT 1
        """)
        row = self.db.execute(sql, {"id": tdp_cadre_id}).mappings().first()
        return dict(row) if row else None

    # ---- helpers ----
    @staticmethod
    def _geo_where(parliament, constituency):
        clauses, params = [], {}
        if parliament:
            clauses.append("f.parliament = :parliament")
            params['parliament'] = parliament
        if constituency:
            clauses.append("f.constituency = :constituency")
            params['constituency'] = constituency
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params
