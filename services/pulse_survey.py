"""Survey Intelligence — profile-driven multi-agency survey engine.

Each agency is a profile: a set of rounds (Jun '25 / Nov '25 / Jun '26) and a
question/option mapping. A round is one survey id, optionally narrowed to a
date window (used for the continuous AP GOVT CATI tracker, SID 30).

  - CODEMO CATI : discrete SIDs 40 / 41 / 42, alliance vote (Q11).
  - AP GOVT CATI: continuous SID 30, date-bucketed; same schema as CODEMO.
  - DHRUVA IVRS : discrete SIDs 43 / 44 / 45, party vote (Q10), MLA agree-scale.
  - CSBS        : placeholder (no data ingested yet).

Vote/CM/satisfaction/MLA come from survey questions; caste / gender / age via the
ivrs_mobiles dimension and the voter roll. build(db, prof) returns a JSON-able
snapshot per agency; materialized + cached like the rest.
"""
from sqlalchemy import text

NOT_ANSWERED = 8

# option-id groups — option ids are global across surveys
GAB = {"Good": [39, 40], "Average": [41], "Bad": [42, 43]}      # very good+good / avg / bad+very bad
AVAIL = {"Good": [54, 52], "Average": [53], "Bad": [55, 56]}    # MLA availability scale (Q16)
AGREE = {"Good": [132, 133], "Average": [], "Bad": [134, 135]}  # DHRUVA agree/disagree (Q40-44)
CM = {33: "Chandrababu", 34: "Jagan", 36: "Pawan Kalyan", 35: "Lokesh"}  # else -> Others
RELIGION = {85: "Hindu", 87: "Muslim", 86: "Christian"}
PARTY_NDA = [14, 29, 30]        # TDP + JanaSena + BJP (party-level)
YSRCP_IDS = [15]

# --- agency profiles -------------------------------------------------------
AGENCIES = [
    {
        "id": "codemo", "name": "CODEMO CATI", "loaded": True,
        "rounds": [{"label": "Jun ’25", "sid": 40}, {"label": "Nov ’25", "sid": 41},
                   {"label": "Jun ’26", "sid": 42}],
        "voteQ": 11, "voteGroups": {"NDA": [28], "YSRCP": YSRCP_IDS},
        "recallQ": 8, "recallGroups": {"NDA": PARTY_NDA, "YSRCP": YSRCP_IDS}, "recallExclude": (NOT_ANSWERED, 44),
        "cmQ": 12, "satQ": 14, "satGroups": GAB,
        "mlaPerfQ": 15, "mlaPerfGroups": GAB, "mlaAvailQ": 16, "mlaAvailGroups": AVAIL,
        "religionQ": 19,
    },
    {
        "id": "apgovt", "name": "AP GOVT CATI", "loaded": True,
        "rounds": [{"label": "Jun ’25", "sid": 30, "from": "2025-05-15", "to": "2025-07-15"},
                   {"label": "Nov ’25", "sid": 30, "from": "2025-10-15", "to": "2025-12-15"},
                   {"label": "Jun ’26", "sid": 30, "from": "2026-04-15", "to": "2026-06-01"}],
        "voteQ": 11, "voteGroups": {"NDA": [28], "YSRCP": YSRCP_IDS},
        "recallQ": 8, "recallGroups": {"NDA": PARTY_NDA, "YSRCP": YSRCP_IDS}, "recallExclude": (NOT_ANSWERED, 44),
        "cmQ": 12, "satQ": 14, "satGroups": GAB,
        "mlaPerfQ": 15, "mlaPerfGroups": GAB, "mlaAvailQ": 16, "mlaAvailGroups": AVAIL,
        "religionQ": 19,
    },
    {
        "id": "dhruva", "name": "DHRUVA IVRS", "loaded": True,
        "rounds": [{"label": "Jun ’25", "sid": 43}, {"label": "Nov ’25", "sid": 44},
                   {"label": "Jun ’26", "sid": 45}],
        "voteQ": 10, "voteGroups": {"NDA": PARTY_NDA, "YSRCP": YSRCP_IDS},  # party-level
        "recallQ": None, "recallGroups": None, "recallExclude": (NOT_ANSWERED,),
        "cmQ": 12, "satQ": 14, "satGroups": GAB,
        "cmRatingQ": 24, "cmRatingGroups": GAB,            # Q24 = CM Satisfaction
        "mlaPerfQ": 43, "mlaPerfGroups": AGREE,            # "MLA - Work and Development"
        "mlaBehaviourQ": 42, "mlaBehaviourGroups": AGREE,  # "MLA - Honest"
        "mlaAvailQ": None, "mlaAvailGroups": AVAIL,
        "religionQ": 19,
    },
    {
        "id": "csds", "name": "CSDS CAPI", "loaded": True,
        "rounds": [{"label": "Jun ’26", "sid": 46}],  # only Jun'26 ingested (CAPI, 152 ACs)
        "voteQ": 10, "voteGroups": {"NDA": PARTY_NDA, "YSRCP": YSRCP_IDS},  # party-level
        "recallQ": 8, "recallGroups": {"NDA": PARTY_NDA, "YSRCP": YSRCP_IDS}, "recallExclude": (NOT_ANSWERED, 44),
        "cmQ": 12, "satQ": 14, "satGroups": GAB,
        "cmRatingQ": 24, "cmRatingGroups": GAB,            # Q24 = CM Satisfaction
        "mlaPerfQ": 15, "mlaPerfGroups": GAB, "mlaAvailQ": None, "mlaAvailGroups": AVAIL,
        "religionQ": 19,
    },
    {
        "id": "ivrs", "name": "IVRS", "loaded": True,
        "rounds": [{"label": "Nov ’25", "sid": 28}, {"label": "Jun ’26", "sid": 31}],  # vote-only; no Jun'25
        "voteQ": 10, "voteGroups": {"NDA": [28], "YSRCP": YSRCP_IDS},  # Q10 carries the alliance option
        "recallQ": None, "recallGroups": None, "recallExclude": (NOT_ANSWERED,),
        "cmQ": None, "satQ": None, "satGroups": GAB,
        "mlaPerfQ": None, "mlaPerfGroups": GAB, "mlaAvailQ": None, "mlaAvailGroups": AVAIL,
        "religionQ": None,
    },
    {"id": "way2media", "name": "Way2Media", "loaded": False},  # placeholder — not in this DB
]

DEFAULT_AGENCY = "codemo"


def agency_meta():
    return [{"id": a["id"], "name": a["name"], "loaded": a.get("loaded", False)} for a in AGENCIES]


def get_profile(agency_id):
    return next((a for a in AGENCIES if a["id"] == (agency_id or DEFAULT_AGENCY) and a.get("rounds")), None)


def _ids(xs):
    return ",".join(str(int(x)) for x in xs) if xs else "-1"


def _rwhere(rnd):
    """Round filter on alias `a`: survey id + optional date window. Returns (sql, params)."""
    s = f"a.ivrs_survey_id={int(rnd['sid'])}"
    p = {}
    if rnd.get("from"):
        s += " AND a.survey_date>=:rfrom"; p["rfrom"] = rnd["from"]
    if rnd.get("to"):
        s += " AND a.survey_date<:rto"; p["rto"] = rnd["to"]
    return s, p


def _share_block(db, rnd, qid, groups, other_label=None, ac=None, exclude=(NOT_ANSWERED,)):
    """Distribution of a question's options into named buckets, as % of answered.
    `ac` scopes to a constituency; `exclude` drops option ids; qid None -> empty block."""
    keys = list(groups.keys()) + ([other_label] if other_label is not None else [])
    if not qid:
        return {"shares": {k: None for k in keys}, "samples": 0}
    rw, p = _rwhere(rnd)
    join = ""
    if ac:
        join = "JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no"; p["cn"] = ac
    acw = " AND im.constituency_name=:cn" if ac else ""
    rows = {r.opt: int(r.n) for r in db.execute(text(
        f"SELECT a.ivrs_option_id opt, COUNT(*) n FROM survey24.ivrs_survey_answer a {join} "
        f"WHERE {rw} AND a.ivrs_question_id={qid} AND a.ivrs_option_id NOT IN ({_ids(exclude)}){acw} "
        f"GROUP BY opt"), p)}
    total = sum(rows.values())
    if total == 0:
        return {"shares": {k: None for k in keys}, "samples": 0}
    out, used = {}, set()
    for name, opts in groups.items():
        out[name] = round(100 * sum(rows.get(o, 0) for o in opts) / total, 1); used.update(opts)
    if other_label is not None:
        out[other_label] = round(100 * sum(v for o, v in rows.items() if o not in used) / total, 1)
    return {"shares": out, "samples": total}


def _cm_block(db, rnd, prof, ac=None):
    qid = prof.get("cmQ")
    if not qid:
        return {"shares": {k: None for k in list(CM.values()) + ["Others"]}, "samples": 0}
    rw, p = _rwhere(rnd)
    join = ""
    if ac:
        join = "JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no"; p["cn"] = ac
    acw = " AND im.constituency_name=:cn" if ac else ""
    rows = {r.opt: int(r.n) for r in db.execute(text(
        f"SELECT a.ivrs_option_id opt, COUNT(*) n FROM survey24.ivrs_survey_answer a {join} "
        f"WHERE {rw} AND a.ivrs_question_id={qid} AND a.ivrs_option_id<>{NOT_ANSWERED}{acw} "
        f"GROUP BY opt"), p)}
    total = sum(rows.values())
    names = list(CM.values()) + ["Others"]
    if total == 0:
        return {"shares": {k: None for k in names}, "samples": 0}
    agg = {name: rows.get(oid, 0) for oid, name in CM.items()}
    agg["Others"] = sum(v for o, v in rows.items() if o not in CM)
    return {"shares": {k: round(100 * v / total, 1) for k, v in agg.items()}, "samples": total}


def _margin_by(db, rnd, prof, col, join, top=None, minn=200, ac=None):
    """NDA-minus-YSRCP margin by a demographic column, for one round.
    `join` must include ivrs_mobiles as `im`; `ac` scopes to a constituency."""
    nda, ys, vq = _ids(prof["voteGroups"]["NDA"]), _ids(prof["voteGroups"]["YSRCP"]), prof["voteQ"]
    rw, p = _rwhere(rnd)
    if ac:
        rw += " AND im.constituency_name=:cn"; p["cn"] = ac
    sql = text(f"""
        SELECT {col} seg,
          SUM(a.ivrs_option_id IN ({nda})) nda, SUM(a.ivrs_option_id IN ({ys})) ysrcp, COUNT(*) total
        FROM survey24.ivrs_survey_answer a {join}
        WHERE {rw} AND a.ivrs_question_id={vq} AND a.ivrs_option_id<>{NOT_ANSWERED}
          AND {col} IS NOT NULL AND {col}<>''
        GROUP BY seg""")
    out = []
    for r in db.execute(sql, p).mappings():
        tot = int(r["total"])
        if tot < minn:
            continue
        out.append({"seg": str(r["seg"]),
                    "margin": round(100 * (int(r["nda"]) - int(r["ysrcp"])) / tot, 1), "samples": tot})
    out.sort(key=lambda x: -x["samples"])
    return out[:top] if top else out


def _religion_margin(db, rnd, prof):
    """Religion margin: join each respondent's vote to their religion (Q19)."""
    relq = prof.get("religionQ")
    if not relq:
        return []
    nda, ys, vq = _ids(prof["voteGroups"]["NDA"]), _ids(prof["voteGroups"]["YSRCP"]), prof["voteQ"]
    rw, p = _rwhere(rnd)
    rw2 = rw.replace("a.ivrs_survey_id", "b.ivrs_survey_id").replace("a.survey_date", "b.survey_date")
    sql = text(f"""
        SELECT rel.opt rid,
          SUM(v.ivrs_option_id IN ({nda})) nda, SUM(v.ivrs_option_id IN ({ys})) ysrcp, COUNT(*) total
        FROM (SELECT a.mobile_no, a.ivrs_option_id FROM survey24.ivrs_survey_answer a
              WHERE {rw} AND a.ivrs_question_id={vq} AND a.ivrs_option_id<>{NOT_ANSWERED}) v
        JOIN (SELECT b.mobile_no, b.ivrs_option_id opt FROM survey24.ivrs_survey_answer b
              WHERE {rw2} AND b.ivrs_question_id={relq}) rel ON rel.mobile_no=v.mobile_no
        GROUP BY rid""")
    out = []
    for r in db.execute(sql, p).mappings():
        name = RELIGION.get(int(r["rid"]))
        if not name:
            continue
        tot = int(r["total"]) or 1
        out.append({"seg": name, "margin": round(100 * (int(r["nda"]) - int(r["ysrcp"])) / tot, 1), "samples": tot})
    order = {"Hindu": 0, "Muslim": 1, "Christian": 2}
    out.sort(key=lambda x: order.get(x["seg"], 9))
    return out


# constituency_id → canonical name overrides for known duplicate/misspelled master rows.
# cid 157 "PRATHIPAD" is the same Guntur AC as cid 212 "PRATHIPADU" (both carry survey
# answers — 157 only in IVRS), so folding it onto the canonical name merges the two at
# build time (raw counts sum under one AC) instead of surfacing a duplicate seat.
CONST_NAME_FIXES = {157: "PRATHIPADU"}


# --- bulk per-constituency passes -----------------------------------------
def _const_name_map(db):
    """Canonical {constituency_id: AC_NAME_UPPER} from the official dakavara constituency
    master (what answer.constituency_id references), with CONST_NAME_FIXES applied for
    known duplicate master rows. True AP coverage in the survey data = 174 distinct ACs."""
    rows = db.execute(text("""
        SELECT constituency_id cid, name nm FROM dakavara_pa.constituency
        WHERE name IS NOT NULL AND name<>''""")).mappings()
    m = {int(r["cid"]): str(r["nm"]).strip().upper() for r in rows}
    for cid, canon in CONST_NAME_FIXES.items():
        if cid in m:
            m[cid] = canon
    return m


def _ac_bulk(db, rnd, qid, groups, namemap, other_label=None, exclude=(NOT_ANSWERED,)):
    """{AC_UPPER: {bucket: pct, '_n': samples}} for a question, keyed by answer.constituency_id."""
    if not qid:
        return {}
    rw, p = _rwhere(rnd)
    sql = text(f"""
        SELECT a.constituency_id cid, a.ivrs_option_id opt, COUNT(*) n
        FROM survey24.ivrs_survey_answer a
        WHERE {rw} AND a.ivrs_question_id={qid} AND a.ivrs_option_id NOT IN ({_ids(exclude)})
          AND a.constituency_id IS NOT NULL
        GROUP BY cid, opt""")
    per = {}
    for r in db.execute(sql, p).mappings():
        cn = namemap.get(int(r["cid"]))
        if not cn:
            continue
        per.setdefault(cn, {})
        per[cn][int(r["opt"])] = per[cn].get(int(r["opt"]), 0) + int(r["n"])
    out = {}
    for cn, opts in per.items():
        total = sum(opts.values()) or 1
        d, used = {}, set()
        for name, os in groups.items():
            d[name] = round(100 * sum(opts.get(o, 0) for o in os) / total, 1); used.update(os)
        if other_label is not None:
            d[other_label] = round(100 * sum(v for o, v in opts.items() if o not in used) / total, 1)
        d["_n"] = sum(opts.values())
        out[cn] = d
    return out


def _ac_cm_bulk(db, rnd, prof, namemap):
    qid = prof.get("cmQ")
    if not qid:
        return {}
    rw, p = _rwhere(rnd)
    sql = text(f"""
        SELECT a.constituency_id cid, a.ivrs_option_id opt, COUNT(*) n
        FROM survey24.ivrs_survey_answer a
        WHERE {rw} AND a.ivrs_question_id={qid} AND a.ivrs_option_id<>{NOT_ANSWERED}
          AND a.constituency_id IS NOT NULL
        GROUP BY cid, opt""")
    per = {}
    for r in db.execute(sql, p).mappings():
        cn = namemap.get(int(r["cid"]))
        if not cn:
            continue
        per.setdefault(cn, {})
        per[cn][int(r["opt"])] = per[cn].get(int(r["opt"]), 0) + int(r["n"])
    out = {}
    for cn, opts in per.items():
        total = sum(opts.values()) or 1
        agg = {name: opts.get(oid, 0) for oid, name in CM.items()}
        agg["Others"] = sum(v for o, v in opts.items() if o not in CM)
        out[cn] = {k: round(100 * v / total, 1) for k, v in agg.items()}
    return out


def _ac_margin_bulk(db, rnd, prof, col, join, namemap, minn=15):
    """NDA-YSRCP margin by a demographic column for ALL constituencies in one pass,
    keyed by answer.constituency_id → official name. Returns {AC_UPPER: {seg: {...}}}."""
    nda, ys, vq = _ids(prof["voteGroups"]["NDA"]), _ids(prof["voteGroups"]["YSRCP"]), prof["voteQ"]
    rw, p = _rwhere(rnd)
    sql = text(f"""
        SELECT a.constituency_id cid, {col} seg,
          SUM(a.ivrs_option_id IN ({nda})) nda, SUM(a.ivrs_option_id IN ({ys})) ysrcp, COUNT(*) total
        FROM survey24.ivrs_survey_answer a {join}
        WHERE {rw} AND a.ivrs_question_id={vq} AND a.ivrs_option_id<>{NOT_ANSWERED}
          AND {col} IS NOT NULL AND {col}<>'' AND a.constituency_id IS NOT NULL
        GROUP BY cid, seg""")
    out = {}
    for r in db.execute(sql, p).mappings():
        tot = int(r["total"])
        if tot < minn:
            continue
        cn = namemap.get(int(r["cid"]))
        if not cn:
            continue
        out.setdefault(cn, {})[str(r["seg"])] = {
            "margin": round(100 * (int(r["nda"]) - int(r["ysrcp"])) / tot, 1), "samples": tot}
    return out


def _ac_religion_bulk(db, rnd, prof, namemap):
    relq = prof.get("religionQ")
    if not relq:
        return {}
    nda, ys, vq = _ids(prof["voteGroups"]["NDA"]), _ids(prof["voteGroups"]["YSRCP"]), prof["voteQ"]
    rw, p = _rwhere(rnd)
    rw2 = rw.replace("a.ivrs_survey_id", "b.ivrs_survey_id").replace("a.survey_date", "b.survey_date")
    sql = text(f"""
        SELECT v.cid cid, rel.opt rid,
          SUM(v.ivrs_option_id IN ({nda})) nda, SUM(v.ivrs_option_id IN ({ys})) ysrcp, COUNT(*) total
        FROM (SELECT a.mobile_no, a.ivrs_option_id, a.constituency_id cid FROM survey24.ivrs_survey_answer a
              WHERE {rw} AND a.ivrs_question_id={vq} AND a.ivrs_option_id<>{NOT_ANSWERED} AND a.constituency_id IS NOT NULL) v
        JOIN (SELECT b.mobile_no, b.ivrs_option_id opt FROM survey24.ivrs_survey_answer b
              WHERE {rw2} AND b.ivrs_question_id={relq}) rel ON rel.mobile_no=v.mobile_no
        GROUP BY cid, rid""")
    acc = {}
    for r in db.execute(sql, p).mappings():
        cn = namemap.get(int(r["cid"]))
        if not cn:
            continue
        name = RELIGION.get(int(r["rid"]), "Others")
        a = acc.setdefault(cn, {}).setdefault(name, [0, 0, 0])
        a[0] += int(r["nda"]); a[1] += int(r["ysrcp"]); a[2] += int(r["total"])
    out = {}
    for cn, names in acc.items():
        d = {name: {"margin": round(100 * (n - y) / t, 1), "samples": t}
             for name, (n, y, t) in names.items() if t >= 10}
        if d:
            out[cn] = d
    return out


def _ac_caste_pop(db, namemap):
    """Caste population share within each constituency, from the mobile roll, keyed by
    constituency_id → official name (agency-independent)."""
    sql = text("""
        SELECT constituency_id cid, caste_name cs, COUNT(*) n
        FROM survey24.ivrs_mobiles
        WHERE constituency_id IS NOT NULL
          AND caste_name IS NOT NULL AND caste_name<>''
        GROUP BY cid, cs""")
    per = {}
    for r in db.execute(sql).mappings():
        cn = namemap.get(int(r["cid"]))
        if not cn:
            continue
        per.setdefault(cn, {})[str(r["cs"])] = per.get(cn, {}).get(str(r["cs"]), 0) + int(r["n"])
    return {cn: {cs: round(100 * n / (sum(cm.values()) or 1), 1) for cs, n in cm.items()}
            for cn, cm in per.items()}


def build_per_ac(db, prof, castePop=None, namemap=None):
    """Per-constituency rating blocks + demographic margins for each round (one agency)."""
    gab = ["Good", "Average", "Bad"]
    cc_join = ("JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no "
               "LEFT JOIN dakavara_pa.caste_state cs ON cs.caste_state_id=im.caste_state_id "
               "LEFT JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id=cs.caste_category_group_id")
    mob_join = "JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no"
    if namemap is None:
        namemap = _const_name_map(db)
    if castePop is None:
        castePop = _ac_caste_pop(db, namemap)
    acs = {}
    for rnd in prof["rounds"]:
        vote = _ac_bulk(db, rnd, prof["voteQ"], prof["voteGroups"], namemap, other_label="Others")
        if not vote:
            continue  # round not ingested
        recall = _ac_bulk(db, rnd, prof.get("recallQ"), prof.get("recallGroups") or {}, namemap,
                          other_label="Others", exclude=prof.get("recallExclude", (NOT_ANSWERED,))) if prof.get("recallQ") else {}
        sat = _ac_bulk(db, rnd, prof.get("satQ"), prof.get("satGroups", GAB), namemap)
        cm = _ac_cm_bulk(db, rnd, prof, namemap)
        cmr = _ac_bulk(db, rnd, prof.get("cmRatingQ"), prof.get("cmRatingGroups", GAB), namemap)
        perf = _ac_bulk(db, rnd, prof.get("mlaPerfQ"), prof.get("mlaPerfGroups", GAB), namemap)
        behav = _ac_bulk(db, rnd, prof.get("mlaBehaviourQ"), prof.get("mlaBehaviourGroups", AGREE), namemap)
        avail = _ac_bulk(db, rnd, prof.get("mlaAvailQ"), prof.get("mlaAvailGroups", AVAIL), namemap)
        cat = _ac_margin_bulk(db, rnd, prof, "cg.caste_category_group_name", cc_join, namemap, minn=15)
        gender = _ac_margin_bulk(db, rnd, prof, "im.gender", mob_join, namemap, minn=15)
        age = _ac_margin_bulk(db, rnd, prof, "im.age_range", mob_join, namemap, minn=15)
        caste = _ac_margin_bulk(db, rnd, prof, "im.caste_name", mob_join, namemap, minn=1)
        relig = _ac_religion_bulk(db, rnd, prof, namemap)
        for cn, v in vote.items():
            nda, ys = v.get("NDA", 0), v.get("YSRCP", 0)
            rv = recall.get(cn, {})
            acs.setdefault(cn, {"ac": cn.title(), "castePop": castePop.get(cn, {}), "rounds": {}})
            acs[cn]["rounds"][rnd["label"]] = {
                "nda": nda, "ysrcp": ys, "others": v.get("Others", 0),
                "margin": round(nda - ys, 1), "winner": "NDA" if nda >= ys else "YSRCP",
                "samples": v["_n"],
                "recall2024": ({"nda": rv.get("NDA"), "ysrcp": rv.get("YSRCP"), "others": rv.get("Others"),
                                "margin": (round(rv["NDA"] - rv["YSRCP"], 1)
                                           if rv.get("NDA") is not None and rv.get("YSRCP") is not None else None)}
                               if rv else None),
                "satisfaction": {k: sat.get(cn, {}).get(k) for k in gab},
                "cmChoice": cm.get(cn, {}),
                "cmRating": ({k: cmr.get(cn, {}).get(k) for k in gab} if prof.get("cmRatingQ") else None),
                "mlaPerformance": {k: perf.get(cn, {}).get(k) for k in gab},
                "mlaBehaviour": ({k: behav.get(cn, {}).get(k) for k in gab} if prof.get("mlaBehaviourQ") else None),
                "mlaAvailability": {k: avail.get(cn, {}).get(k) for k in gab},
                "casteCategory": cat.get(cn, {}),
                "gender": gender.get(cn, {}),
                "age": age.get(cn, {}),
                "caste": caste.get(cn, {}),
                "religion": relig.get(cn, {}),
            }
    return sorted(acs.values(), key=lambda a: a["ac"])


def build(db, prof, castePop=None, namemap=None):
    """Compute the full statewide Survey Intelligence snapshot for one agency."""
    cc_join = ("JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no "
               "LEFT JOIN dakavara_pa.caste_state cs ON cs.caste_state_id=im.caste_state_id "
               "LEFT JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id=cs.caste_category_group_id")
    mob_join = "JOIN survey24.ivrs_mobiles im ON im.mobile_no=a.mobile_no"
    per_round = []
    for rnd in prof["rounds"]:
        vote = _share_block(db, rnd, prof["voteQ"], prof["voteGroups"], other_label="Others")
        loaded = vote["samples"] > 0
        nda, ys = vote["shares"].get("NDA"), vote["shares"].get("YSRCP")
        recall = (_share_block(db, rnd, prof["recallQ"], prof["recallGroups"], other_label="Others",
                               exclude=prof.get("recallExclude", (NOT_ANSWERED,)))["shares"]
                  if prof.get("recallQ") else {"NDA": None, "YSRCP": None, "Others": None})
        rn, ryy = recall.get("NDA"), recall.get("YSRCP")
        per_round.append({
            "label": rnd["label"], "loaded": loaded, "voteShare": vote, "samples": vote["samples"],
            "ndaLead": (round(nda - ys, 1) if (nda is not None and ys is not None) else None),
            "recall2024": recall,
            "recallLead": (round(rn - ryy, 1) if (rn is not None and ryy is not None) else None),
            "satisfaction": _share_block(db, rnd, prof.get("satQ"), prof.get("satGroups", GAB))["shares"],
            "cmChoice": _cm_block(db, rnd, prof)["shares"],
            "cmRating": (_share_block(db, rnd, prof["cmRatingQ"], prof.get("cmRatingGroups", GAB))["shares"] if prof.get("cmRatingQ") else None),
            "mlaPerformance": _share_block(db, rnd, prof.get("mlaPerfQ"), prof.get("mlaPerfGroups", GAB))["shares"],
            "mlaBehaviour": (_share_block(db, rnd, prof["mlaBehaviourQ"], prof.get("mlaBehaviourGroups", AGREE))["shares"] if prof.get("mlaBehaviourQ") else None),
            "mlaAvailability": _share_block(db, rnd, prof.get("mlaAvailQ"), prof.get("mlaAvailGroups", AVAIL))["shares"],
            "casteCategory": _margin_by(db, rnd, prof, "cg.caste_category_group_name", cc_join, minn=300) if loaded else [],
            "caste": _margin_by(db, rnd, prof, "im.caste_name", mob_join, top=15, minn=300) if loaded else [],
            "gender": _margin_by(db, rnd, prof, "im.gender", mob_join, minn=300) if loaded else [],
            "religion": _religion_margin(db, rnd, prof) if loaded else [],
        })
    return {
        "agencyId": prof["id"],
        "source": f"{prof['name']} — rounds " + " / ".join(r["label"] for r in prof["rounds"]),
        "agencies": agency_meta(),
        "rounds": [r["label"] for r in prof["rounds"]],
        "perRound": per_round,
        "perAC": build_per_ac(db, prof, castePop=castePop, namemap=namemap),
    }


def build_all(db):
    """Build every loaded agency's snapshot. Returns {agencyId: snapshot}."""
    namemap = _const_name_map(db)
    castePop = _ac_caste_pop(db, namemap)
    out = {}
    for prof in AGENCIES:
        if prof.get("rounds") and prof.get("loaded"):
            out[prof["id"]] = build(db, prof, castePop=castePop, namemap=namemap)
    return out
