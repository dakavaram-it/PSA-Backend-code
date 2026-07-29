"""Read-only verified-voter access for the SIR DASHBOARD, over **mytdp.booth_voter**.

Live SIR (Special Intensive Revision) voter verification is tracked per-voter:

    booth_voter.sir_verified     1 = voter verified         booth_voter.sir_verified_by  -> active user
    booth_voter.sir_status       available/death/shift/...   booth_voter.updated_at       -> today / yesterday / trend
    booth_voter.booth_id -> booth.id -> booth.assembly_id -> assembly.name (AC)

`sir_verified` is indexed, so all of these are fast even on the ~81M-row table.

"Total Voters" is NOT computed here: COUNT(DISTINCT voter_id) over booth_voter takes
minutes. Instead the per-AC electoral-roll totals come from dakavara_pa
(SirReferenceRepository, fast + cached), and the SirDashboardService merges the two
by **AC name** (mytdp assembly name == dakavara AC name).
"""
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session


class SirDashboardRepository:
    def __init__(self, sir_db: Session):
        self.db = sir_db

    # ---- date window for a named range ----
    @staticmethod
    def range_window(range_, frm=None, to=None, today=None):
        t = today or date.today()
        if range_ == "today":
            return t.isoformat(), t.isoformat()
        if range_ == "yesterday":
            y = (t - timedelta(days=1)).isoformat()
            return y, y
        if range_ == "custom":
            return frm, to
        return None, None  # overall

    @staticmethod
    def _verified_where(frm, to):
        clauses = ["bv.sir_verified = 1"]
        params = {}
        if frm:
            clauses.append("DATE(bv.updated_at) >= :frm"); params["frm"] = frm
        if to:
            clauses.append("DATE(bv.updated_at) <= :to"); params["to"] = to
        return " AND ".join(clauses), params

    # ---- verified + active users per AC (keyed by UPPER(AC name) to merge with the
    #      dakavara_pa reference), within an optional date window ----
    def verified_by_ac(self, frm=None, to=None):
        where, params = self._verified_where(frm, to)
        rows = self.db.execute(text(f"""
            SELECT UPPER(TRIM(a.name)) AS acKey,
                   COUNT(*) AS verified,
                   COUNT(DISTINCT bv.sir_verified_by) AS activeUsers
            FROM booth_voter bv
            JOIN booth b    ON b.id = bv.booth_id
            JOIN assembly a ON a.id = b.assembly_id
            WHERE {where}
            GROUP BY UPPER(TRIM(a.name))
        """), params).mappings().all()
        return {r["acKey"]: {"verified": int(r["verified"]), "activeUsers": int(r["activeUsers"])}
                for r in rows}

    # ---- scalar verified + active users (whole state, in a window) ----
    def verified_totals(self, frm=None, to=None):
        where, params = self._verified_where(frm, to)
        r = self.db.execute(text(f"""
            SELECT COUNT(*) AS verified, COUNT(DISTINCT bv.sir_verified_by) AS activeUsers
            FROM booth_voter bv WHERE {where}"""), params).mappings().first()
        return {"verified": int(r["verified"] or 0), "activeUsers": int(r["activeUsers"] or 0)}

    # ---- 14-day verification trend ----
    def trend(self, days=14):
        rows = self.db.execute(text("""
            SELECT DATE(bv.updated_at) AS d, COUNT(*) AS verified
            FROM booth_voter bv
            WHERE bv.sir_verified = 1 AND bv.updated_at >= (CURDATE() - INTERVAL :days DAY)
            GROUP BY DATE(bv.updated_at) ORDER BY d
        """), {"days": days}).mappings().all()
        return [{"date": str(r["d"]), "verified": int(r["verified"])} for r in rows]

    # ---- verified voters by SIR status (available / death / shift / duplicate ...) ----
    def status_split(self, frm=None, to=None):
        where, params = self._verified_where(frm, to)
        rows = self.db.execute(text(f"""
            SELECT COALESCE(NULLIF(bv.sir_status,''), 'unknown') AS status, COUNT(*) AS count
            FROM booth_voter bv WHERE {where}
            GROUP BY bv.sir_status ORDER BY count DESC"""), params).mappings().all()
        return [{"status": r["status"], "count": int(r["count"])} for r in rows]

    # =================================================================
    # CUBS / D2D — per-voter field collection (all from booth_voter, no BLO)
    # =================================================================
    # Collection-metric SELECT expressions shared by the scalar + per-AC queries.
    _CUBS_METRICS = """
        COUNT(*) AS visited,
        COALESCE(SUM(bv.is_enumiration_form_submitted = 1), 0) AS formsSubmitted,
        COALESCE(SUM(bv.sir_mobile_number IS NOT NULL AND bv.sir_mobile_number <> ''), 0) AS mobileCollected,
        COALESCE(SUM(bv.sir_caste_id IS NOT NULL AND bv.sir_caste_id <> ''), 0) AS casteCollected,
        COALESCE(SUM(bv.sir_political_party_id IS NOT NULL AND bv.sir_political_party_id <> ''), 0) AS partyCollected,
        COUNT(DISTINCT bv.sir_verified_by) AS activeUsers,
        COALESCE(SUM(bv.sir_status = 'available'), 0)       AS available,
        COALESCE(SUM(bv.sir_status = 'death'), 0)           AS death,
        COALESCE(SUM(bv.sir_status = 'temporary shift'), 0) AS temporaryShift,
        COALESCE(SUM(bv.sir_status = 'permanent shift'), 0) AS permanentShift,
        COALESCE(SUM(bv.sir_status = 'duplicate'), 0)       AS duplicate,
        COALESCE(SUM(bv.sir_status = 'double vote'), 0)     AS doubleVote
    """
    _CUBS_INT_KEYS = ("visited", "formsSubmitted", "mobileCollected", "casteCollected",
                      "partyCollected", "activeUsers", "available", "death",
                      "temporaryShift", "permanentShift", "duplicate", "doubleVote")

    def cubs_totals(self, frm=None, to=None):
        """State-wide CUBS collection totals (visited + each collected field + status)."""
        where, params = self._verified_where(frm, to)
        r = self.db.execute(text(f"SELECT {self._CUBS_METRICS} FROM booth_voter bv WHERE {where}"),
                            params).mappings().first()
        return {k: int(r[k] or 0) for k in self._CUBS_INT_KEYS}

    def cubs_by_ac(self, frm=None, to=None):
        """Per-AC CUBS metrics, keyed by UPPER(AC name) to merge with the reference."""
        where, params = self._verified_where(frm, to)
        rows = self.db.execute(text(f"""
            SELECT UPPER(TRIM(a.name)) AS acKey, {self._CUBS_METRICS}
            FROM booth_voter bv
            JOIN booth b    ON b.id = bv.booth_id
            JOIN assembly a ON a.id = b.assembly_id
            WHERE {where}
            GROUP BY UPPER(TRIM(a.name))
        """), params).mappings().all()
        return {r["acKey"]: {k: int(r[k] or 0) for k in self._CUBS_INT_KEYS} for r in rows}

    def party_split(self, frm=None, to=None):
        """Verified voters by captured political party (UUID -> code/name)."""
        where, params = self._verified_where(frm, to)
        rows = self.db.execute(text(f"""
            SELECT pp.code AS code, pp.description AS name, COUNT(*) AS count
            FROM booth_voter bv
            JOIN political_party pp ON pp.id = bv.sir_political_party_id
            WHERE {where}
            GROUP BY pp.id, pp.code, pp.description ORDER BY count DESC"""), params).mappings().all()
        return [{"code": r["code"], "name": r["name"], "count": int(r["count"])} for r in rows]

    def caste_category_split(self, frm=None, to=None):
        """Verified voters by captured caste category (OC / BC / SC / ST / MINORITY)."""
        where, params = self._verified_where(frm, to)
        rows = self.db.execute(text(f"""
            SELECT cc.code AS code, COUNT(*) AS count
            FROM booth_voter bv
            JOIN caste_category cc ON cc.id = bv.sir_caste_category
            WHERE {where}
            GROUP BY cc.id, cc.code ORDER BY count DESC"""), params).mappings().all()
        return [{"code": r["code"], "count": int(r["count"])} for r in rows]
