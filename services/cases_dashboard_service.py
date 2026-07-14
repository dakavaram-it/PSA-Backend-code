"""Business logic + leader scoring for the Cases dashboard.

Scoring is deliberately transparent: a 0-100 weighted sum of explainable
signals, each capped so no single factor dominates. Every leader response
ships its own component breakdown + human-readable pattern tags so the UI can
answer "why does this person score what they score".
"""
import re
from core.config import get_settings

_IMG_BASE = get_settings().cadre_image_base_url

# crime_head is a mix of readable labels and bare IPC section numbers (e.g. '307.0').
# Map known sections to readable names; keep text labels as-is.
_CRIME_LABELS = {
    "188": "Disobeying Order (188)",
    "353": "Assault on Public Servant (353)",
    "307": "Attempt to Murder (307)",
    "171": "Election Offence (171)",
    "302": "Murder (302)",
    "306": "Abetment of Suicide (306)",
    "ndpsa": "NDPS / Drugs",
    "sc/st": "SC/ST Atrocities",
}


# IPC / Act section -> plain-English offence (covers the common sections in this data)
_SECTION_DESC = {
    "34": "common intention", "109": "abetment", "120": "criminal conspiracy",
    "120b": "criminal conspiracy", "143": "unlawful assembly", "147": "rioting",
    "148": "armed rioting", "149": "unlawful assembly (common object)",
    "153": "provocation to riot", "153a": "promoting enmity between groups",
    "171": "election offence", "188": "disobeying a public order",
    "269": "negligent act likely to spread infection", "270": "malignant spread of infection",
    "271": "breaking quarantine rules", "290": "public nuisance",
    "294": "obscene acts", "302": "murder", "304": "culpable homicide",
    "306": "abetment of suicide", "307": "attempt to murder", "308": "attempt to culpable homicide",
    "323": "voluntarily causing hurt", "324": "hurt by dangerous weapon",
    "326": "grievous hurt by dangerous weapon", "332": "hurt to deter a public servant",
    "341": "wrongful restraint", "342": "wrongful confinement",
    "353": "assault to deter a public servant", "354": "assault on a woman (outraging modesty)",
    "365": "kidnapping/abduction", "376": "rape", "384": "extortion",
    "395": "dacoity", "396": "dacoity with murder", "397": "robbery with deadly weapon",
    "420": "cheating", "427": "mischief causing damage", "447": "criminal trespass",
    "448": "house trespass", "452": "house trespass with intent to hurt",
    "467": "forgery of valuable security", "468": "forgery for cheating",
    "471": "using a forged document", "504": "intentional insult provoking breach of peace",
    "505": "statements causing public mischief", "506": "criminal intimidation",
    "509": "insulting a woman's modesty", "152a": "assaulting public servant",
}


def describe_sections(sections_str):
    """Build a readable sentence from a case's parsed sections string."""
    if not sections_str:
        return None
    names, seen, scst = [], set(), False
    for token in str(sections_str).split(","):
        t = token.strip()
        if "SC/ST" in t.upper():
            scst = True
            continue
        head = t.split()[0] if t.split() else ""   # section part before the Act name
        m = re.match(r"(\d+[a-z]?)", head.lower())   # 447 / 120b / 153a (sub-clauses dropped)
        if not m:
            continue
        base = m.group(1)
        desc = _SECTION_DESC.get(base)
        if desc and desc not in seen:
            seen.add(desc); names.append(desc)
    parts = []
    if names:
        parts.append(", ".join(names[:6]).capitalize())
    if scst:
        parts.append("atrocities against SC/ST community")
    if not parts:
        return None
    return " — incl. ".join(parts) if len(parts) > 1 else parts[0]


def _crime_label(raw):
    if raw is None or str(raw).strip() in ("", "Unknown"):
        return "Unknown"
    t = str(raw).strip()
    key = t.lower()
    if key in _CRIME_LABELS:
        return _CRIME_LABELS[key]
    num = re.sub(r"\.0$", "", t)        # '307.0' -> '307'
    if num in _CRIME_LABELS:
        return _CRIME_LABELS[num]
    if num.isdigit():                    # unmapped bare section number
        return f"Section {num}"
    return t

# (key, weight-per-unit, cap) — contributions sum then clamp to 100
_WEIGHTS = [
    ("totalCases",     6, 36),   # volume of cases
    ("seriousCases",  10, 30),   # attempt-murder / SC-ST / grievous etc.
    ("ncCases",        8, 24),   # non-compoundable (not privately settleable)
    ("ptCases",        5, 20),   # actively pending trial
    ("policeStations", 4, 12),   # spread across jurisdictions
]


def _photo_url(photo):
    if not photo:
        return None
    if str(photo).startswith("http"):
        return photo
    return f"{_IMG_BASE}/{photo}"


def _score(row):
    """Return (score, band, components[], patterns[])."""
    comps = []
    total = 0
    for key, w, cap in _WEIGHTS:
        n = int(row.get(key) or 0)
        pts = min(n * w, cap)
        total += pts
        comps.append({"factor": key, "count": n, "points": pts, "max": cap})
    score = min(100, total)
    band = "High" if score >= 60 else "Medium" if score >= 30 else "Low"

    tc = int(row.get("totalCases") or 0)
    patterns = []
    if tc >= 3:
        patterns.append({"tag": "Repeat Offender", "detail": f"{tc} cases on record"})
    if int(row.get("seriousCases") or 0) >= 1:
        patterns.append({"tag": "Serious Charges",
                         "detail": f"{row['seriousCases']} case(s) with grave/atrocity sections"})
    if int(row.get("ncCases") or 0) >= 1:
        patterns.append({"tag": "Non-Compoundable",
                         "detail": f"{row['ncCases']} case(s) cannot be privately settled"})
    if int(row.get("ptCases") or 0) >= 1:
        patterns.append({"tag": "Active Litigation",
                         "detail": f"{row['ptCases']} case(s) pending trial"})
    if int(row.get("policeStations") or 0) >= 2:
        patterns.append({"tag": "Multi-Jurisdiction",
                         "detail": f"spread across {row['policeStations']} police stations"})
    if tc and int(row.get("disposedCases") or 0) == tc:
        patterns.append({"tag": "All Disposed", "detail": "every case resolved/closed"})
    return score, band, comps, patterns


class CasesDashboardService:
    def __init__(self, repo):
        self.repo = repo

    def geo_tree(self):
        rows = self.repo.geo_tree()
        tree = {}
        for r in rows:
            p = r["parliament"]
            tree.setdefault(p, {"parliament": p, "cases": 0, "constituencies": []})
            tree[p]["cases"] += int(r["cases"] or 0)
            if r["constituency"]:
                tree[p]["constituencies"].append(
                    {"name": r["constituency"], "cases": int(r["cases"] or 0)})
        # sort constituencies (most cases first) within each parliament, then parliaments
        for t in tree.values():
            t["constituencies"].sort(key=lambda c: -c["cases"])
        return sorted(tree.values(), key=lambda x: -x["cases"])

    def overview(self, parliament=None, constituency=None):
        data = self.repo.overview(parliament, constituency)
        # normalize crime_head labels and re-aggregate (so '307.0' -> readable, merged)
        merged = {}
        for row in data.get("byCrimeHead", []):
            label = _crime_label(row["label"])
            merged[label] = merged.get(label, 0) + int(row["value"] or 0)
        data["byCrimeHead"] = [{"label": k, "value": v}
                               for k, v in sorted(merged.items(), key=lambda x: -x[1])]
        return data

    @staticmethod
    def _map_leader(r):
        score, band, comps, patterns = _score(r)
        return {
            "leaderKey": r["leaderKey"],
            "tdpCadreId": r["tdpCadreId"],
            "isCadre": r["tdpCadreId"] is not None,
            "name": r["name"],
            "mid": r.get("matchedMid"),
            "designation": r["cadreDesignation"] or r["designation"],
            "party": r["party"],
            "mobile": r["mobile"],
            "photoUrl": _photo_url(r["photo"]),
            "parliament": r["parliament"],
            "constituency": r["constituency"],
            "totalCases": int(r["totalCases"] or 0),
            "seriousCases": int(r["seriousCases"] or 0),
            "ncCases": int(r["ncCases"] or 0),
            "ptCases": int(r["ptCases"] or 0),
            "disposedCases": int(r["disposedCases"] or 0),
            "uiCases": int(r["uiCases"] or 0),
            "policeStations": int(r["policeStations"] or 0),
            "score": score,
            "riskBand": band,
            "patterns": patterns,
        }

    def leaders(self, parliament=None, constituency=None, scope="leaders", limit=200):
        out = [self._map_leader(r) for r in self.repo.leaders(parliament, constituency, scope, limit)]
        out.sort(key=lambda x: (-x["totalCases"], -x["score"]))
        return out

    def search(self, q, limit=50):
        if not q or len(q.strip()) < 2:
            return []
        out = [self._map_leader(r) for r in self.repo.search(q, limit)]
        out.sort(key=lambda x: (-x["totalCases"], -x["score"]))
        return out

    def leader_detail(self, leader_key):
        cases = self.repo.leader_cases(leader_key)
        if not cases:
            return None
        # rebuild aggregate row for scoring from the case list
        agg = {
            "totalCases": len(cases),
            "ptCases": sum(c["status"] == "PT" for c in cases),
            "disposedCases": sum(c["status"] == "D" for c in cases),
            "uiCases": sum(c["status"] == "UI" for c in cases),
            "ncCases": sum(c["cnc"] == "NC" for c in cases),
            "seriousCases": sum(bool(c["isSerious"]) for c in cases),
            "policeStations": len({c["policeStation"] for c in cases}),
        }
        score, band, comps, patterns = _score(agg)
        cadre = None
        if leader_key.startswith("c:"):
            cadre = self.repo.cadre_meta(int(leader_key[2:]))
            if cadre:
                cadre["photoUrl"] = _photo_url(cadre.pop("photo", None))
        fallback_name = leader_key[2:].title() if leader_key.startswith("n:") else "Unknown"
        return {
            "leaderKey": leader_key,
            "name": (cadre or {}).get("name") or fallback_name,
            "cadre": cadre,
            "party": cases[0]["party"],
            "designation": cases[0]["designation"],
            "score": score,
            "riskBand": band,
            "scoreBreakdown": comps,
            "patterns": patterns,
            "stats": agg,
            "cases": [{
                "caseId": c["caseId"], "firNo": c["firNo"], "policeStation": c["policeStation"],
                "parliament": c["parliament"], "constituency": c["constituency"],
                "district": c["district"], "crimeHead": c["crimeHead"], "status": c["status"],
                "cnc": c["cnc"], "caseStatus": c["caseStatus"], "court": c["court"],
                "isSerious": bool(c["isSerious"]), "sections": c["sections"], "sectionRaw": c["sectionRaw"],
                "description": describe_sections(c["sections"]),
            } for c in cases],
        }
