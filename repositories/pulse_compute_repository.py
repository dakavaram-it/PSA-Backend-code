"""Heavy DB computations for the Pulse Trend refresh. Run on demand (refresh
endpoint / background), not on the request path. Reads survey24 (+ dakavara
for caste category). Config-driven via pulse_config."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from services import pulse_config as cfg


def _ids(xs):
    return ",".join(str(int(x)) for x in xs)


class PulseComputeRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    def _bloc_counts(self, sids):
        """(TDP/NDA, YSRCP, Others) response counts across a set of surveys."""
        sql = text(f"""
            SELECT SUM(ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tn,
                   SUM(ivrs_option_id = {cfg.OPT_YSRCP}) ys,
                   SUM(ivrs_option_id = {cfg.OPT_OTHERS}) ot
            FROM survey24.ivrs_survey_answer
            WHERE ivrs_survey_id IN ({_ids(sids)}) AND ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
        """)
        r = self.db.execute(sql).first()
        return [int(r[0] or 0), int(r[1] or 0), int(r[2] or 0)]

    def ivrs_waves(self):
        return [{"label": w["label"], "sids": "+".join(map(str, w["sids"])),
                 "counts": self._bloc_counts(w["sids"])} for w in cfg.IVRS_WAVES]

    def cati_monthly(self):
        sql = text(f"""
            SELECT LEFT(survey_date,7) ym,
                   SUM(ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tn,
                   SUM(ivrs_option_id = {cfg.OPT_YSRCP}) ys,
                   SUM(ivrs_option_id = {cfg.OPT_OTHERS}) ot
            FROM survey24.ivrs_survey_answer
            WHERE ivrs_survey_id = {int(cfg.CATI_SURVEY_ID)}
              AND ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
              AND survey_date >= :frm
            GROUP BY ym ORDER BY ym
        """)
        return {r.ym: [int(r.tn or 0), int(r.ys or 0), int(r.ot or 0)]
                for r in self.db.execute(sql, {"frm": cfg.CATI_FROM}).mappings()}

    def member_split_by_wave(self):
        """Cadre/Public (TDP/NDA, YSRCP, Others) per wave — for trend + split."""
        before, after = cfg.DECLINE_BEFORE_SIDS, cfg.DECLINE_AFTER_SIDS
        sql = text(f"""
            SELECT (CASE WHEN a.ivrs_survey_id IN ({_ids(before)}) THEN 'before' ELSE 'after' END) wave,
                   COALESCE(im.member_type,'Unknown') mt,
                   SUM(a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tn,
                   SUM(a.ivrs_option_id = {cfg.OPT_YSRCP}) ys,
                   SUM(a.ivrs_option_id = {cfg.OPT_OTHERS}) ot
            FROM survey24.ivrs_survey_answer a
            LEFT JOIN survey24.ivrs_mobiles im ON im.mobile_no = a.mobile_no
            WHERE a.ivrs_survey_id IN ({_ids(before + after)}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
            GROUP BY wave, mt
        """)
        out = {"before": {}, "after": {}}
        for r in self.db.execute(sql).mappings():
            out[r["wave"]][r["mt"]] = [int(r["tn"] or 0), int(r["ys"] or 0), int(r["ot"] or 0)]
        return out

    def cadre_by_year(self):
        sql = text("""
            SELECT enrollment_year yr, COUNT(*) members FROM tdp_cadre
            WHERE is_deleted='N' AND enrollment_year BETWEEN 2009 AND 2030
            GROUP BY enrollment_year ORDER BY enrollment_year
        """)
        return [{"year": int(r.yr), "members": int(r.members)} for r in self.db.execute(sql).mappings()]

    def constituency_detail(self, name):
        """Everything for one constituency, from a single scoped query that dedupes
        to one row per (voter, wave) — so counts are UNIQUE VOTERS, not responses.
        Returns:
          dims:   {dim: [{seg, mt, nB,tB,nA,tA}]}  (nB/nA = unique voters per wave,
                  tB/tA = TDP/NDA voters) for booth/caste/caste_category/age/gender
          houses: {} placeholder — households come from the bulk roll join (bulk_houses),
                  not this per-seat query, so the lazy path stays fast.
        Scoped to one seat, so the join stays small and fast."""
        before_set = set(cfg.DECLINE_BEFORE_SIDS)
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        # No SQL window function (it sorts the whole join and was dropping the
        # connection on big seats). Fetch the joined answer rows and dedup to one
        # per (voter, wave) in Python — the join alone is the only real cost.
        sql = text(f"""
            SELECT a.mobile_no, a.survey_date, a.ivrs_survey_id sid,
                   (a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tdp,
                   COALESCE(im.member_type,'Unknown') mt,
                   im.part_no, im.caste_name, im.age_range, im.gender,
                   cg.caste_category_group_name cat
            FROM survey24.ivrs_mobiles im
            JOIN survey24.ivrs_survey_answer a ON a.mobile_no = im.mobile_no
            LEFT JOIN dakavara_pa.caste_state cs ON cs.caste_state_id = im.caste_state_id
            LEFT JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id = cs.caste_category_group_id
            WHERE im.constituency_name = :c
              AND a.ivrs_survey_id IN ({before},{after}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
        """)
        # dedup: per (mobile, wave) keep the latest answer (by survey_date, sid)
        best = {}
        for r in self.db.execute(sql, {"c": name}).mappings():
            wave = "B" if r["sid"] in before_set else "A"
            key = (r["mobile_no"], wave)
            order = (str(r["survey_date"] or ""), int(r["sid"]))
            cur = best.get(key)
            if cur is None or order > cur[0]:
                best[key] = (order, r, wave)

        DIMS = {"booth": "part_no", "caste": "caste_name", "caste_category": "cat",
                "age": "age_range", "gender": "gender"}
        acc = {d: {} for d in DIMS}
        overall = {}  # member_type -> [nB,tB,nA,tA] over ALL voters (seat headline)
        for _order, r, wave in best.values():
            tdp = 1 if r["tdp"] else 0
            for dim, col in DIMS.items():
                seg = r[col]
                if seg is None or seg == "":
                    continue
                cell = acc[dim].setdefault((str(seg), r["mt"]), [0, 0, 0, 0])
                if wave == "B":
                    cell[0] += 1; cell[1] += tdp
                else:
                    cell[2] += 1; cell[3] += tdp
            ov = overall.setdefault(r["mt"], [0, 0, 0, 0])
            if wave == "B":
                ov[0] += 1; ov[1] += tdp
            else:
                ov[2] += 1; ov[3] += tdp

        houses = {"stayed": 0, "moved": 0, "total": 0, "covered": 0}
        dims = {dim: [{"seg": k[0], "mt": k[1], "nB": v[0], "tB": v[1], "nA": v[2], "tA": v[3]}
                      for k, v in cells.items()] for dim, cells in acc.items()}
        # seat headline totals, stored as a pseudo-dimension (seg = member_type)
        dims["overall"] = [{"seg": mt, "mt": mt, "nB": v[0], "tB": v[1], "nA": v[2], "tA": v[3]}
                           for mt, v in overall.items()]
        return {"dims": dims, "houses": houses}

    def _distinct_pass(self, dim, col, extra_join=""):
        """One bulk pass: unique-voter TDP/NDA counts per (constituency, seg, member_type)
        across ALL seats, via COUNT(DISTINCT). STRAIGHT_JOIN drives from the (smaller)
        answers table. Yields (cn, dim, seg, mt, nB,tB,nA,tA)."""
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        tn = _ids(cfg.TDP_NDA_OPTS)
        sql = text(f"""
            SELECT im.constituency_name cn, {col} seg, COALESCE(im.member_type,'Unknown') mt,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({before}) THEN a.mobile_no END) nB,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({before}) AND a.ivrs_option_id IN ({tn}) THEN a.mobile_no END) tB,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({after}) THEN a.mobile_no END) nA,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({after}) AND a.ivrs_option_id IN ({tn}) THEN a.mobile_no END) tA
            FROM survey24.ivrs_survey_answer a
            STRAIGHT_JOIN survey24.ivrs_mobiles im ON im.mobile_no = a.mobile_no {extra_join}
            WHERE a.ivrs_survey_id IN ({before},{after}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
              AND im.constituency_name IS NOT NULL AND im.constituency_name <> ''
              AND {col} IS NOT NULL AND {col} <> ''
            GROUP BY cn, seg, mt
        """)
        for r in self.db.execute(sql).mappings():
            yield (r["cn"].strip().upper(), dim, str(r["seg"]), r["mt"],
                   int(r["nB"]), int(r["tB"]), int(r["nA"]), int(r["tA"]))

    def bulk_segments(self):
        """All seats × all dims (+ overall) in 6 bulk passes. Yields segment rows
        (cn, dim, seg, mt, nB,tB,nA,tA) for pulse_trend_segment_decline."""
        cc_join = ("LEFT JOIN dakavara_pa.caste_state cs ON cs.caste_state_id = im.caste_state_id "
                   "LEFT JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id = cs.caste_category_group_id")
        for dim, col, join in (
            ("booth", "im.part_no", ""), ("caste", "im.caste_name", ""),
            ("age", "im.age_range", ""), ("gender", "im.gender", ""),
            ("caste_category", "cg.caste_category_group_name", cc_join),
        ):
            yield from self._distinct_pass(dim, col, join)
        # overall (seat headline) — seg = member_type
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        tn = _ids(cfg.TDP_NDA_OPTS)
        sql = text(f"""
            SELECT im.constituency_name cn, COALESCE(im.member_type,'Unknown') mt,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({before}) THEN a.mobile_no END) nB,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({before}) AND a.ivrs_option_id IN ({tn}) THEN a.mobile_no END) tB,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({after}) THEN a.mobile_no END) nA,
              COUNT(DISTINCT CASE WHEN a.ivrs_survey_id IN ({after}) AND a.ivrs_option_id IN ({tn}) THEN a.mobile_no END) tA
            FROM survey24.ivrs_survey_answer a
            STRAIGHT_JOIN survey24.ivrs_mobiles im ON im.mobile_no = a.mobile_no
            WHERE a.ivrs_survey_id IN ({before},{after}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
              AND im.constituency_name IS NOT NULL AND im.constituency_name <> ''
            GROUP BY cn, mt
        """)
        for r in self.db.execute(sql).mappings():
            yield (r["cn"].strip().upper(), "overall", r["mt"], r["mt"],
                   int(r["nB"]), int(r["tB"]), int(r["nA"]), int(r["tA"]))

    def bulk_houses(self):
        """All seats' households via the electoral roll (m_main_voter_details), joined
        to IVRS answers by MOBILE_NO (the survey key — voter_id doesn't match the roll).
        Household = (PART_NO, HOUSE_NO) within the constituency. Returns
        {CONSTITUENCY: {stayed, moved, total, covered}} for households that backed
        TDP/NDA before and were re-surveyed after."""
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        tn = _ids(cfg.TDP_NDA_OPTS)
        sql = text(f"""
            SELECT m.Constituency_name cn, CONCAT(m.PART_NO,'-',m.HOUSE_NO) hh,
              MAX(CASE WHEN a.ivrs_survey_id IN ({before}) THEN (a.ivrs_option_id IN ({tn})) END) bt,
              MAX(CASE WHEN a.ivrs_survey_id IN ({after})  THEN (a.ivrs_option_id IN ({tn})) END) at_,
              SUM(a.ivrs_survey_id IN ({before})) bc, SUM(a.ivrs_survey_id IN ({after})) ac
            FROM survey24.ivrs_survey_answer a
            JOIN dakavara_pa.m_main_voter_details m ON m.MOBILE_NO = a.mobile_no
            WHERE a.ivrs_survey_id IN ({before},{after}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
              AND m.Constituency_name IS NOT NULL AND m.Constituency_name <> ''
              AND m.HOUSE_NO IS NOT NULL AND m.HOUSE_NO <> ''
            GROUP BY cn, hh
        """)
        out = {}
        for r in self.db.execute(sql).mappings():
            if not r["cn"] or not r["bc"] or not r["ac"]:
                continue
            cn = r["cn"].strip().upper()
            agg = out.setdefault(cn, [0, 0, 0])
            agg[2] += 1
            if r["bt"] == 1 and r["at_"] == 1:
                agg[0] += 1
            elif r["bt"] == 1 and r["at_"] == 0:
                agg[1] += 1
        return {cn: {"stayed": v[0], "moved": v[1], "total": v[0] + v[1], "covered": v[2]}
                for cn, v in out.items()}

    def list_constituencies(self):
        """All constituency names present in the IVRS data (UPPER, matches the drill)."""
        rows = self.db.execute(text(
            "SELECT DISTINCT constituency_name FROM survey24.ivrs_mobiles "
            "WHERE constituency_name IS NOT NULL AND constituency_name <> '' "
            "ORDER BY constituency_name")).all()
        return [r[0].strip().upper() for r in rows]

    def constituency_leader(self, name):
        """MLA / constituency incharge for a constituency (small live lookup).
        Prefers a sitting MLA, else the highest incharge. Photo handled as an
        initials avatar client-side (candidate.image is only a bare filename)."""
        sql = text("""
            SELECT candidate_name, designation, mobile_no, tdp_cadre_id
            FROM dakavara_pa.constituency_mla_incharge
            WHERE is_deleted = 'N' AND UPPER(constituency_name) = UPPER(:c)
            ORDER BY (designation = 'MLA') DESC, id ASC
            LIMIT 1
        """)
        r = self.db.execute(sql, {"c": name}).mappings().first()
        if not r:
            return None
        return {"name": r["candidate_name"], "designation": r["designation"],
                "mobile": r["mobile_no"]}

    def segment_decline_all(self):
        """Booth/caste/age/gender decline for EVERY constituency, in 4 full-table
        passes (GROUP BY constituency + segment + member_type). Yields tuples
        (constituency, dim, seg, member_type, nB, tB, nA, tA) for the materialized
        pulse_trend_segment_decline table — replaces per-click on-demand compute."""
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        for dim, c in cfg.CONSTITUENCY_DIMS.items():
            col = c["col"]
            sql = text(f"""
                SELECT im.constituency_name cn, im.{col} seg, COALESCE(im.member_type,'Unknown') mt,
                  SUM(a.ivrs_survey_id IN ({before})) nB,
                  SUM(a.ivrs_survey_id IN ({before}) AND a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tB,
                  SUM(a.ivrs_survey_id IN ({after})) nA,
                  SUM(a.ivrs_survey_id IN ({after}) AND a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tA
                FROM survey24.ivrs_mobiles im
                JOIN survey24.ivrs_survey_answer a ON a.mobile_no = im.mobile_no
                WHERE a.ivrs_survey_id IN ({before},{after}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
                  AND im.constituency_name IS NOT NULL AND im.constituency_name <> ''
                  AND im.{col} IS NOT NULL AND im.{col} <> ''
                GROUP BY cn, seg, mt
            """)
            for r in self.db.execute(sql).mappings():
                yield (r["cn"], dim, str(r["seg"]), r["mt"],
                       int(r["nB"] or 0), int(r["tB"] or 0), int(r["nA"] or 0), int(r["tA"] or 0))

    def precheck(self):
        """Validate the data the refresh depends on. Returns a list of
        {name, ok, critical, detail}; refresh aborts if any critical check fails."""
        checks = []

        def add(name, ok, critical, detail):
            checks.append({"name": name, "ok": bool(ok), "critical": critical, "detail": str(detail)})

        try:
            self.db.execute(text("SELECT 1"))
        except Exception as e:
            add("Database connectivity", False, True, str(e)[:140])
            return checks
        add("Database connectivity", True, True, "reachable")

        b = self.db.execute(text(
            f"SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id IN ({_ids(cfg.DECLINE_BEFORE_SIDS)})")).scalar()
        add(f"Before-wave responses (SIDs {','.join(map(str, cfg.DECLINE_BEFORE_SIDS))})",
            (b or 0) > 0, True, f"{b:,} responses")
        a = self.db.execute(text(
            f"SELECT COUNT(*) FROM survey24.ivrs_survey_answer WHERE ivrs_survey_id IN ({_ids(cfg.DECLINE_AFTER_SIDS)})")).scalar()
        add(f"After-wave responses (SIDs {','.join(map(str, cfg.DECLINE_AFTER_SIDS))})",
            (a or 0) > 0, True, f"{a:,} responses")

        opt = self.db.execute(text(
            f"SELECT COUNT(DISTINCT ivrs_option_id) FROM survey24.ivrs_survey_answer "
            f"WHERE ivrs_survey_id IN ({_ids(cfg.DECLINE_BEFORE_SIDS + cfg.DECLINE_AFTER_SIDS)}) "
            f"AND ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})")).scalar()
        add("Party option ids present", (opt or 0) >= 2, True,
            f"{opt} of {len(cfg.PARTY_OPTS)} configured option ids seen")

        cati = self.db.execute(text(
            f"SELECT COUNT(*) FROM survey24.ivrs_survey_answer "
            f"WHERE ivrs_survey_id = {int(cfg.CATI_SURVEY_ID)} AND survey_date >= :f"),
            {"f": cfg.CATI_FROM}).scalar()
        add(f"CATI tracker data (SID {cfg.CATI_SURVEY_ID})", (cati or 0) > 0, False,
            f"{cati:,} responses since {cfg.CATI_FROM}")

        geo = self.db.execute(text(
            "SELECT COUNT(*) FROM survey24.ivrs_mobiles "
            "WHERE constituency_name IS NOT NULL AND constituency_name <> ''")).scalar()
        add("Mobiles geo-tagged to constituencies", (geo or 0) > 0, False, f"{geo:,} mobiles")
        return checks

    def panel_transition(self):
        """Voter-level switching: among mobiles answering BOTH waves, how each
        moved between TDP/NDA / YSRCP / Others. Returns rows {mt, bb, ab, n}.
        One choice per mobile per wave = their latest answer in that wave."""
        before, after = _ids(cfg.DECLINE_BEFORE_SIDS), _ids(cfg.DECLINE_AFTER_SIDS)
        bloc = (f"CASE WHEN ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)}) THEN 'TN' "
                f"WHEN ivrs_option_id = {cfg.OPT_YSRCP} THEN 'YS' ELSE 'OT' END")

        def pick(sids):
            return (f"SELECT mobile_no, bloc FROM (SELECT mobile_no, {bloc} bloc, "
                    f"ROW_NUMBER() OVER (PARTITION BY mobile_no ORDER BY survey_date DESC, ivrs_survey_id DESC) rn "
                    f"FROM survey24.ivrs_survey_answer "
                    f"WHERE ivrs_survey_id IN ({sids}) AND ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)}) "
                    f"AND mobile_no IS NOT NULL AND mobile_no <> '') t WHERE rn = 1")

        sql = text(f"""
            SELECT COALESCE(im.member_type,'Unknown') mt, b.bloc bb, a.bloc ab, COUNT(*) n
            FROM ({pick(before)}) b
            JOIN ({pick(after)}) a ON a.mobile_no = b.mobile_no
            LEFT JOIN survey24.ivrs_mobiles im ON im.mobile_no = b.mobile_no
            GROUP BY mt, b.bloc, a.bloc
        """)
        return [dict(r) for r in self.db.execute(sql).mappings()]

    def decline_dim(self, col, mn, extra_join=""):
        """Per-segment TDP/NDA share before/after, by audience (all/cadre/public)."""
        before, after = cfg.DECLINE_BEFORE_SIDS, cfg.DECLINE_AFTER_SIDS
        sql = text(f"""
            SELECT {col} seg, COALESCE(im.member_type,'Unknown') mt,
              SUM(a.ivrs_survey_id IN ({_ids(before)})) nB,
              SUM(a.ivrs_survey_id IN ({_ids(before)}) AND a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tB,
              SUM(a.ivrs_survey_id IN ({_ids(after)})) nA,
              SUM(a.ivrs_survey_id IN ({_ids(after)}) AND a.ivrs_option_id IN ({_ids(cfg.TDP_NDA_OPTS)})) tA
            FROM survey24.ivrs_survey_answer a
            JOIN survey24.ivrs_mobiles im ON im.mobile_no = a.mobile_no {extra_join}
            WHERE a.ivrs_survey_id IN ({_ids(before + after)}) AND a.ivrs_option_id IN ({_ids(cfg.PARTY_OPTS)})
              AND {col} IS NOT NULL AND {col} <> ''
            GROUP BY seg, mt
        """)
        return [dict(r) for r in self.db.execute(sql).mappings()]
