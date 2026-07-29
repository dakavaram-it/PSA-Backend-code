"""Shapes Pulse Trend data. IVRS/CATI/decline come from a refreshable snapshot
(disk cache via pulse_cache, falling back to the embedded seed below). The
election-tab data (party vote-share / turnout) stays live (small, fast queries).
A refresh recomputes the snapshot from the DB via PulseComputeRepository."""
from sqlalchemy import text

from app.services.pulse_decline_data import DECLINE, CALCULATED_AT, DATA_THROUGH
from app.services import pulse_cache, pulse_config as cfg

YEARS = ["2014", "2019", "2024"]
PARTY_LABEL = {"TDP": "TDP", "YSRC": "YSRCP", "JANASENA": "JSP", "BJP": "BJP", "INC": "INC", "TRS": "TRS"}
MAJOR = ["TDP", "YSRC", "JANASENA", "BJP", "INC"]
SERIES_ORDER = ["TDP / NDA", "YSRCP", "Others"]
_MONTH = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
          "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── embedded seed snapshot (used until the first refresh writes a cache file) ──
_SEED = {
    "ivrsWaves": [
        {"label": "Apr–May '24", "sids": "19+20+21+24+25+26", "counts": [2834589, 1536232, 249493]},
        {"label": "Dec '25", "sids": "28", "counts": [804399, 297833, 123520]},
    ],
    "member": {
        "before": {"Cadre": [1753472, 540787, 104942], "Public": [1081116, 995445, 144551]},
        "after": {"Cadre": [540392, 125298, 72766], "Public": [264007, 172535, 50754]},
    },
    "beforeLabel": "Apr–May '24", "afterLabel": "Dec '25",
    "catiMonthly": {
        "2025-04": [13251, 7431, 2357], "2025-05": [115236, 59239, 11344], "2025-06": [165927, 67930, 15356],
        "2025-07": [222421, 77749, 17969], "2025-08": [198493, 62736, 15390], "2025-09": [195238, 62931, 14172],
        "2025-10": [124183, 41581, 9164], "2025-11": [164807, 54287, 10254], "2025-12": [174671, 58455, 9290],
        "2026-01": [103813, 36048, 5967], "2026-02": [84683, 29553, 5373], "2026-03": [105027, 38183, 7162],
        "2026-04": [98553, 36218, 7165], "2026-05": [65653, 25022, 5220],
    },
    "cadreByYear": [
        {"year": 2010, "members": 1154723}, {"year": 2012, "members": 1396912},
        {"year": 2014, "members": 18677236}, {"year": 2016, "members": 33816},
    ],
    "decline": DECLINE,
    "panel": {
        "all": {"total": 485625, "matrix": {
            "TN": {"TN": 271308, "YS": 25546, "OT": 29754},
            "YS": {"TN": 32804, "YS": 94801, "OT": 10702},
            "OT": {"TN": 11422, "YS": 5020, "OT": 4268}}},
        "cadre": {"total": 288612, "matrix": {
            "TN": {"TN": 192687, "YS": 14234, "OT": 20437},
            "YS": {"TN": 17374, "YS": 29423, "OT": 4467},
            "OT": {"TN": 6229, "YS": 1867, "OT": 1894}}},
        "public": {"total": 197013, "matrix": {
            "TN": {"TN": 78621, "YS": 11312, "OT": 9317},
            "YS": {"TN": 15430, "YS": 65378, "OT": 6235},
            "OT": {"TN": 5193, "YS": 3153, "OT": 2374}}},
    },
    "declineThrough": DATA_THROUGH,
    "calculatedAt": CALCULATED_AT + " (seed)",
}

# bloc codes -> display labels (panel matrix)
PANEL_BLOCS = [["TN", "TDP / NDA"], ["YS", "YSRCP"], ["OT", "Others"]]


def _snap():
    return pulse_cache.load() or _SEED


class PulseService:
    def __init__(self, repo):
        self.repo = repo

    # ── Election tab (live) ──
    def overview(self):
        return {"years": YEARS, "parties": self._parties(), "turnout": self._turnout(), "cadre": self._cadre()}

    def _parties(self):
        idx = {(r["party"], str(r["yr"])): r for r in self.repo.party_trend()}
        series = []
        for code in MAJOR:
            points = []
            for y in YEARS:
                r = idx.get((code, y))
                points.append({"year": y, "share": _num(r["share"]) if r else None,
                               "votes": int(r["votes"]) if r and r["votes"] else None,
                               "seats": int(r["seats"]) if r and r["seats"] is not None else None})
            if any(p["share"] is not None for p in points):
                series.append({"party": PARTY_LABEL.get(code, code), "points": points})
        return series

    def _turnout(self):
        rows = {str(r["yr"]): r for r in self.repo.turnout_trend()}
        return [{"year": y, "turnout": _num(rows[y]["turnout"]) if y in rows else None,
                 "votesPolled": int(rows[y]["votesPolled"]) if rows.get(y) and rows[y]["votesPolled"] else None,
                 "constituencies": int(rows[y]["consts"]) if y in rows else None} for y in YEARS]

    def _cadre(self):
        by_year = _snap().get("cadreByYear", [])
        return {"byYear": by_year, "totalEnrolled": sum(b["members"] for b in by_year),
                "note": "Cadre enrolment is concentrated in membership-drive years, not election years."}

    # ── IVRS / CATI / decline (from snapshot) ──
    def ivrs(self):
        s = _snap()
        order = SERIES_ORDER
        waves = s["ivrsWaves"]

        trend = []
        for i, series in enumerate(order):
            pts = [{"survey": w["label"], "count": w["counts"][i],
                    "share": round(100 * w["counts"][i] / (sum(w["counts"]) or 1), 1)} for w in waves]
            trend.append({"party": series, "points": pts})

        after = s["member"]["after"]
        member_split = [{"party": series, "cadre": after.get("Cadre", [0, 0, 0])[i],
                         "public": after.get("Public", [0, 0, 0])[i]} for i, series in enumerate(order)]

        wave_labels = [s.get("beforeLabel", waves[0]["label"]), s.get("afterLabel", waves[-1]["label"])]
        member_trend = []
        for group in ("Cadre", "Public"):
            pts = []
            for key, lbl in (("before", wave_labels[0]), ("after", wave_labels[1])):
                v = s["member"][key].get(group, [0, 0, 0])
                pts.append({"survey": lbl, "share": round(100 * v[0] / (sum(v) or 1), 1), "total": sum(v)})
            member_trend.append({"group": group, "points": pts})

        months = sorted(s["catiMonthly"])
        cati_labels = [f"{_MONTH[m[5:7]]} '{m[2:4]}" for m in months]
        cati_series = [{"party": series, "points": [{"month": m,
                        "share": round(100 * s["catiMonthly"][m][i] / (sum(s["catiMonthly"][m]) or 1), 1)} for m in months]}
                       for i, series in enumerate(order)]

        return {
            "surveys": [{"label": w["label"], "sids": w["sids"]} for w in waves],
            "latestLabel": waves[-1]["label"],
            "splitLabel": wave_labels[1],
            "sampleSizes": [{"survey": w["label"], "total": sum(w["counts"])} for w in waves],
            "trend": trend, "memberSplit": member_split, "memberTrend": member_trend,
            "cati": {"labels": cati_labels, "series": cati_series},
            "decline": s["decline"],
            "panel": s.get("panel"),
            "panelBlocs": PANEL_BLOCS,
            "beforeLabel": wave_labels[0],
            "afterLabel": wave_labels[1],
            "declineMeta": {"calculatedAt": s.get("calculatedAt", "—"), "dataThrough": s.get("declineThrough", "")},
        }


# ─────────────────── refresh (recompute snapshot from DB) ───────────────────
def _shape_decline(rows, top, mn):
    """rows: [{seg, mt, nB, tB, nA, tA}] -> {all/cadre/public: [{seg,shareA,shareC,delta}]}"""
    by_seg = {}
    for r in rows:
        seg = str(r["seg"]); mt = r["mt"]
        d = by_seg.setdefault(seg, {"all": [0, 0, 0, 0]})
        vals = [int(r["nB"] or 0), int(r["tB"] or 0), int(r["nA"] or 0), int(r["tA"] or 0)]
        for i in range(4):
            d["all"][i] += vals[i]
            d.setdefault(mt, [0, 0, 0, 0])[i] += vals[i]

    def rows_for(key):
        out = []
        for seg, d in by_seg.items():
            v = d.get(key)
            if not v:
                continue
            nB, tB, nA, tA = v
            if nB < mn or nA < mn:
                continue
            sB, sA = 100 * tB / nB, 100 * tA / nA
            out.append({"seg": seg, "shareA": round(sB, 1), "shareC": round(sA, 1), "delta": round(sA - sB, 1)})
        out.sort(key=lambda x: x["delta"])
        return out[:top] if top else out

    return {"all": rows_for("all"), "cadre": rows_for("Cadre"), "public": rows_for("Public")}


def _shape_panel(rows):
    """rows: [{mt, bb, ab, n}] -> {all/cadre/public: {total, matrix{bb:{ab:n}}}}"""
    blocs = ["TN", "YS", "OT"]
    acc = {aud: {b: {a: 0 for a in blocs} for b in blocs} for aud in ("all", "cadre", "public")}
    for r in rows:
        bb, ab, n = r["bb"], r["ab"], int(r["n"] or 0)
        if bb not in blocs or ab not in blocs:
            continue
        acc["all"][bb][ab] += n
        if r["mt"] == "Cadre":
            acc["cadre"][bb][ab] += n
        elif r["mt"] == "Public":
            acc["public"][bb][ab] += n
    return {aud: {"total": sum(acc[aud][b][a] for b in blocs for a in blocs), "matrix": acc[aud]}
            for aud in acc}


def _shape_constituency_dims(dims_detail):
    """dims_detail: {dim: [{seg,mt,nB,tB,nA,tA}]} (nB/nA = unique voters per wave)
    -> {all/cadre/public: {dim: [{seg, shareA, shareC, delta, votersB, votersA}]}}.
    Sorted by biggest TDP/NDA decline; segments below the per-dim min are dropped."""
    out = {"all": {}, "cadre": {}, "public": {}}
    for dim, c in cfg.CONSTITUENCY_DIMS.items():
        by_seg = {}
        for r in dims_detail.get(dim, []):
            seg = str(r["seg"]); mt = r["mt"]
            d = by_seg.setdefault(seg, {"all": [0, 0, 0, 0]})
            vals = [int(r["nB"]), int(r["tB"]), int(r["nA"]), int(r["tA"])]
            for i in range(4):
                d["all"][i] += vals[i]
                d.setdefault(mt, [0, 0, 0, 0])[i] += vals[i]

        def rows_for(key):
            res = []
            for seg, d in by_seg.items():
                v = d.get(key)
                if not v:
                    continue
                nB, tB, nA, tA = v
                if nB < c["min"] or nA < c["min"]:
                    continue
                res.append({"seg": (f"Booth {seg}" if dim == "booth" else seg),
                            "shareA": round(100 * tB / nB, 1), "shareC": round(100 * tA / nA, 1),
                            "delta": round(100 * tA / nA - 100 * tB / nB, 1),
                            "votersB": nB, "votersA": nA})
            res.sort(key=lambda x: x["delta"])
            return res[:c["top"]] if c["top"] else res

        for aud, key in (("all", "all"), ("cadre", "Cadre"), ("public", "Public")):
            out[aud][dim] = rows_for(key)
    return out


def _summary_from_overall(rows):
    """overall pseudo-dim rows -> {all/cadre/public: {votersB, votersA, shareB, shareC, delta}}."""
    agg = {"all": [0, 0, 0, 0]}
    for r in rows:
        mt = r["mt"]
        for i, k in enumerate(("nB", "tB", "nA", "tA")):
            agg["all"][i] += int(r[k]); agg.setdefault(mt, [0, 0, 0, 0])[i] += int(r[k])

    def one(key):
        v = agg.get(key)
        if not v or not v[0] or not v[2]:
            return None
        nB, tB, nA, tA = v
        sB, sA = 100 * tB / nB, 100 * tA / nA
        return {"votersB": nB, "votersA": nA, "shareB": round(sB, 1),
                "shareC": round(sA, 1), "delta": round(sA - sB, 1)}

    return {"all": one("all"), "cadre": one("Cadre"), "public": one("Public")}


# in-process response cache: the DB is ~400ms/round-trip away (cross-region), and the
# materialized data is stable between refreshes, so we read each seat from the DB once
# and serve it from memory thereafter (instant). Cleared on refresh via invalidate().
_CV_CACHE = {}        # key -> (ts, response)
_CV_TTL = 1800        # safety re-read after 30 min even without an explicit invalidate


def invalidate_constituency_views():
    _CV_CACHE.clear()


def _shape_constituency_response(cn, dims, houses, leader):
    return {"constituency": titleCase(cn),
            "decline": _shape_constituency_dims(dims),
            "summary": _summary_from_overall(dims.get("overall", [])),
            "houses": houses or {"total": 0, "stayed": 0, "moved": 0, "covered": 0},
            "leader": leader, "source": "materialized",
            "meta": {"beforeLabel": cfg.DECLINE_BEFORE_LABEL, "afterLabel": cfg.DECLINE_AFTER_LABEL}}


def preload_constituency_views():
    """Load EVERY constituency into the in-process cache in 3 bulk queries (segments,
    houses, leaders) and shape them — so first opens are instant too, not just repeats.
    Meant to run in a background thread at startup / after a refresh."""
    import time as _t
    from app.database.db import dakavara_session
    t0 = _t.time()
    seg, houses, leaders = {}, {}, {}
    with dakavara_session() as db:
        for r in db.execute(text(
            "SELECT constituency_name cn, dim, seg, member_type mt, n_before nB, "
            "tdp_before tB, n_after nA, tdp_after tA FROM pulse_trend_segment_decline")).mappings():
            seg.setdefault(r["cn"], {}).setdefault(r["dim"], []).append(
                {"seg": r["seg"], "mt": r["mt"], "nB": r["nB"], "tB": r["tB"], "nA": r["nA"], "tA": r["tA"]})
        for r in db.execute(text(
            "SELECT constituency_name cn, total, stayed, moved, covered "
            "FROM pulse_trend_constituency_houses")).mappings():
            houses[r["cn"]] = {"total": r["total"], "stayed": r["stayed"], "moved": r["moved"], "covered": r["covered"]}
        for r in db.execute(text(
            "SELECT constituency_name cn, candidate_name, designation, mobile_no "
            "FROM dakavara_pa.constituency_mla_incharge WHERE is_deleted='N' "
            "ORDER BY (designation='MLA') DESC, id ASC")).mappings():
            cn = (r["cn"] or "").strip().upper()
            if cn and cn not in leaders:
                leaders[cn] = {"name": r["candidate_name"], "designation": r["designation"], "mobile": r["mobile_no"]}
    now = _t.time()
    built = {cn: (now, _shape_constituency_response(cn, dims, houses.get(cn), leaders.get(cn)))
             for cn, dims in seg.items()}
    _CV_CACHE.clear(); _CV_CACHE.update(built)
    return len(built), round(_t.time() - t0, 1)


def constituency_view(compute, name):
    """Everything for one constituency, served from an in-process cache. First open
    reads the materialized pulse_trend_* rows (or computes live on a miss); subsequent
    opens are instant from memory. Cache is cleared on refresh."""
    import time as _t
    key = (name or "").strip().upper()
    hit = _CV_CACHE.get(key)
    if hit and (_t.time() - hit[0]) < _CV_TTL:
        return hit[1]

    detail = pulse_cache.read_constituency(key)
    source = "materialized"
    if detail is None:
        detail = compute.constituency_detail(key)
        pulse_cache.write_constituency(key, detail["dims"], detail["houses"])
        source = "live"
    resp = {"constituency": titleCase(key),
            "decline": _shape_constituency_dims(detail["dims"]),
            "summary": _summary_from_overall(detail["dims"].get("overall", [])),
            "houses": detail["houses"],
            "leader": compute.constituency_leader(key),
            "source": source,
            "meta": {"beforeLabel": cfg.DECLINE_BEFORE_LABEL, "afterLabel": cfg.DECLINE_AFTER_LABEL}}
    _CV_CACHE[key] = (_t.time(), resp)
    return resp


def titleCase(s):
    return " ".join(w.capitalize() for w in str(s).split())


def build_snapshot(compute):
    """Recompute the full IVRS/CATI/decline snapshot from the DB."""
    snap = {
        "ivrsWaves": compute.ivrs_waves(),
        "catiMonthly": compute.cati_monthly(),
        "member": compute.member_split_by_wave(),
        "beforeLabel": cfg.DECLINE_BEFORE_LABEL,
        "afterLabel": cfg.DECLINE_AFTER_LABEL,
        "cadreByYear": compute.cadre_by_year(),
        "declineThrough": cfg.DECLINE_AFTER_LABEL,
    }
    dec = {"all": {}, "cadre": {}, "public": {}}
    for dim, c in cfg.DECLINE_DIMS.items():
        shaped = _shape_decline(compute.decline_dim(c["col"], c["min"]), c["top"], c["min"])
        for aud in dec:
            dec[aud][dim] = shaped[aud]
    cc = _shape_decline(compute.decline_dim(
        "cg.caste_category_group_name", cfg.CASTE_CATEGORY_MIN,
        "JOIN dakavara_pa.caste_state cs ON cs.caste_state_id=im.caste_state_id "
        "JOIN dakavara_pa.caste_category_group cg ON cg.caste_category_group_id=cs.caste_category_group_id"),
        None, cfg.CASTE_CATEGORY_MIN)
    for aud in dec:
        dec[aud]["caste_category"] = cc[aud]
    snap["decline"] = dec
    snap["panel"] = _shape_panel(compute.panel_transition())
    return snap
