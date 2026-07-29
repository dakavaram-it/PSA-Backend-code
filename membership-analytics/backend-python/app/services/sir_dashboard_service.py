"""Business logic for the SIR DASHBOARD (read-only voter-verification analytics).

Composes the screenshot's three areas:
  * Overall Status cards  -> cumulative totals + today / yesterday
  * Analytics             -> 14-day trend + verified/pending split
  * Parliament-wise / Assembly-wise tables -> per-PC / per-AC for a selected range

Two sources, merged by AC name:
  * mytdp.booth_voter (SirDashboardRepository) -> verified / active users (live, fast)
  * dakavara_pa       (SirReferenceRepository) -> per-AC electoral-roll totals + PC name
                                                  (the authoritative 25 PCs / 175 ACs)
"""

RANGES = ["today", "yesterday", "overall", "custom"]


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


class SirDashboardService:
    def __init__(self, repo, reference_repo):
        self.repo = repo               # mytdp booth_voter (verified side)
        self.ref = reference_repo      # dakavara_pa AC reference (totals + PC names)

    # ---- top cards + analytics (range-independent) ----
    def overview(self):
        reference = self.ref.ac_reference()
        total_voters = sum(r["totalVoters"] for r in reference.values())
        cumulative = self.repo.verified_totals()           # all-time verified (fast)
        today = self.repo.verified_totals(*self._win("today"))
        yesterday = self.repo.verified_totals(*self._win("yesterday"))
        verified = cumulative["verified"]
        pending = max(0, total_voters - verified)
        return {
            "totalVoters": total_voters,
            "verified": verified,
            "pending": pending,
            "verifiedPct": _pct(verified, total_voters),
            "pendingPct": _pct(pending, total_voters),
            "today": today,
            "yesterday": yesterday,
            "trend": self.repo.trend(14),
            "statusSplit": self.repo.status_split(),
        }

    # ---- Parliament-wise (PC) table for a range ----
    def parliament(self, range_="today", frm=None, to=None):
        reference = self.ref.ac_reference()
        verified_by_ac = self.repo.verified_by_ac(*self._win(range_, frm, to))
        pcs = {}
        for ac_key, ref in reference.items():
            pc = ref.get("pcName") or "Unknown"
            row = pcs.setdefault(pc, {"pc": pc, "totalVoters": 0, "verified": 0, "activeUsers": 0})
            row["totalVoters"] += ref.get("totalVoters", 0)
            v = verified_by_ac.get(ac_key)
            if v:
                row["verified"] += v["verified"]
                row["activeUsers"] += v["activeUsers"]
        return {"range": range_, "pcCount": len(pcs), "rows": self._finish(pcs.values())}

    # ---- Assembly-wise (AC) table for a range (optionally within one PC) ----
    def assembly(self, range_="today", frm=None, to=None, pc=None):
        reference = self.ref.ac_reference()
        verified_by_ac = self.repo.verified_by_ac(*self._win(range_, frm, to))
        rows = []
        for ac_key, ref in reference.items():
            if pc and (ref.get("pcName") or "") != pc:
                continue
            v = verified_by_ac.get(ac_key, {})
            rows.append({"ac": ref.get("acName"), "acNo": ref.get("acNo"), "pc": ref.get("pcName"),
                         "totalVoters": ref.get("totalVoters", 0),
                         "verified": v.get("verified", 0), "activeUsers": v.get("activeUsers", 0)})
        return {"range": range_, "acCount": len(rows), "rows": self._finish(rows)}

    # =================================================================
    # CUBS / D2D — per-voter field collection (booth_voter only, no BLO)
    # =================================================================
    def cubs_overview(self, range_="overall", frm=None, to=None):
        win = self._win(range_, frm, to)
        totals = self.repo.cubs_totals(*win)
        return {
            "range": range_,
            "totals": totals,
            "today": self.repo.cubs_totals(*self._win("today")),
            "yesterday": self.repo.cubs_totals(*self._win("yesterday")),
            "statusSplit": self.repo.status_split(*win),
            "partySplit": self.repo.party_split(*win),
            "casteCategorySplit": self.repo.caste_category_split(*win),
        }

    def cubs_parliament(self, range_="today", frm=None, to=None):
        reference = self.ref.ac_reference()
        by_ac = self.repo.cubs_by_ac(*self._win(range_, frm, to))
        pcs = {}
        for ac_key, ref in reference.items():
            pc = ref.get("pcName") or "Unknown"
            row = pcs.setdefault(pc, self._zero_cubs({"pc": pc, "totalVoters": 0}))
            row["totalVoters"] += ref.get("totalVoters", 0)
            self._add_cubs(row, by_ac.get(ac_key))
        return {"range": range_, "pcCount": len(pcs), "rows": self._finish_cubs(pcs.values())}

    def cubs_assembly(self, range_="today", frm=None, to=None, pc=None):
        reference = self.ref.ac_reference()
        by_ac = self.repo.cubs_by_ac(*self._win(range_, frm, to))
        rows = []
        for ac_key, ref in reference.items():
            if pc and (ref.get("pcName") or "") != pc:
                continue
            row = self._zero_cubs({"ac": ref.get("acName"), "acNo": ref.get("acNo"),
                                   "pc": ref.get("pcName"), "totalVoters": ref.get("totalVoters", 0)})
            self._add_cubs(row, by_ac.get(ac_key))
            rows.append(row)
        return {"range": range_, "acCount": len(rows), "rows": self._finish_cubs(rows)}

    _CUBS_KEYS = ("visited", "formsSubmitted", "mobileCollected", "casteCollected",
                  "partyCollected", "activeUsers", "available", "death",
                  "temporaryShift", "permanentShift", "duplicate", "doubleVote")

    @classmethod
    def _zero_cubs(cls, base):
        for k in cls._CUBS_KEYS:
            base[k] = 0
        return base

    @classmethod
    def _add_cubs(cls, row, metrics):
        if not metrics:
            return
        for k in cls._CUBS_KEYS:
            row[k] += metrics.get(k, 0)

    @staticmethod
    def _finish_cubs(rows):
        out = []
        for r in rows:
            total = r.get("totalVoters", 0)
            r["pending"] = max(0, total - r.get("visited", 0))
            r["visitedPct"] = _pct(r.get("visited", 0), total)
            out.append(r)
        out.sort(key=lambda x: x.get("visited", 0), reverse=True)
        return out

    # ---- helpers ----
    def _win(self, range_, frm=None, to=None):
        return self.repo.range_window(range_, frm, to)

    @staticmethod
    def _finish(rows):
        out = []
        for r in rows:
            total = r.get("totalVoters", 0)
            verified = r.get("verified", 0)
            r["pending"] = max(0, total - verified)
            r["verifiedPct"] = _pct(verified, total)
            r["pendingPct"] = _pct(total - verified, total)
            out.append(r)
        out.sort(key=lambda x: x.get("verified", 0), reverse=True)
        return out
