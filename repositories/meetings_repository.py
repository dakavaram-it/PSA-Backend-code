"""Read-only data access for the Meetings dashboard, over **dakavara_pa**.

The party meetings system lives in the `party_meeting*` family (the listed
`meetings` / `meeting_*` names are a planned normalized schema; the live
source-of-truth is `party_meeting*`):

    party_meeting              -- the meeting record (name, type, level, dates, conducted)
    party_meeting_type         -- 31 types, grouped under party_meeting_main_type (4)
    party_meeting_level        -- STATE / DISTRICT / CONSTITUENCY / MANDAL / ... (9)
    party_meeting_occurrence   -- ONCE / MULTIPLE
    party_meeting_invitee      -- invited cadre/representatives
    party_meeting_attendance   -- attendance marks (per session)
    party_meeting_minute       -- minutes / action points (MoM / ATR)
    party_meeting_document     -- uploaded files

Data notes baked into the queries:
  * `start_date` is the live time dimension; `conducted_date` is stale (≈2022),
    so the dashboard keys off start_date.
  * Some rows carry garbage dates (year 14, year 1) — a sane floor of 2015-01-01
    and a near-future ceiling are always applied.
  * Supporting data (invitees, attendance, minutes, docs) is largely historical;
    it's surfaced as totals/per-row counts, not assumed present on recent rows.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

# Drop garbage dates and far-future noise on every query.
_DATE_FLOOR = "2015-01-01"

# Base joins shared by overview + listing (labels for type / main-type / level / occurrence).
_BASE = """
    FROM party_meeting m
    LEFT JOIN party_meeting_type t       ON t.party_meeting_type_id = m.party_meeting_type_id
    LEFT JOIN party_meeting_main_type mt ON mt.party_meeting_main_type_id = t.party_meeting_main_type_id
    LEFT JOIN party_meeting_level l      ON l.party_meeting_level_id = m.party_meeting_level_id
    LEFT JOIN party_meeting_occurrence o ON o.party_meeting_occurrence_id = m.party_meeting_occurrence_id
"""


class MeetingsRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    @staticmethod
    def _where(filters):
        clauses = ["m.start_date >= :floor", "m.start_date <= (CURDATE() + INTERVAL 1 YEAR)"]
        params = {"floor": _DATE_FLOOR}
        f = filters or {}
        if f.get("from"):
            clauses.append("m.start_date >= :from"); params["from"] = f["from"]
        if f.get("to"):
            clauses.append("m.start_date <= :to"); params["to"] = f["to"]
        if f.get("mainType"):
            clauses.append("t.party_meeting_main_type_id = :mainType"); params["mainType"] = f["mainType"]
        if f.get("type"):
            clauses.append("m.party_meeting_type_id = :type"); params["type"] = f["type"]
        if f.get("level"):
            clauses.append("m.party_meeting_level_id = :level"); params["level"] = f["level"]
        if f.get("occurrence"):
            clauses.append("m.party_meeting_occurrence_id = :occurrence"); params["occurrence"] = f["occurrence"]
        conducted = f.get("conducted")
        if conducted == "Y":
            clauses.append("m.is_conducted = 'Y'")
        elif conducted == "N":
            clauses.append("m.is_conducted = 'N'")
        elif conducted == "pending":
            clauses.append("m.is_conducted IS NULL")
        if f.get("ivr") == "Y":
            clauses.append("m.is_conducted_by_ivr = 'Y'")
        if f.get("q"):
            clauses.append("m.meeting_name LIKE :q"); params["q"] = f"%{f['q']}%"
        return "WHERE " + " AND ".join(clauses), params

    # ---- dropdown options ----
    def filter_options(self):
        main_types = self.db.execute(text("""
            SELECT mt.party_meeting_main_type_id AS id, mt.meeting_type AS name,
                   COUNT(m.party_meeting_id) AS meetings
            FROM party_meeting_main_type mt
            LEFT JOIN party_meeting_type t ON t.party_meeting_main_type_id = mt.party_meeting_main_type_id
            LEFT JOIN party_meeting m ON m.party_meeting_type_id = t.party_meeting_type_id
                 AND m.start_date >= :floor
            GROUP BY mt.party_meeting_main_type_id, mt.meeting_type ORDER BY meetings DESC
        """), {"floor": _DATE_FLOOR}).mappings().all()
        types = self.db.execute(text("""
            SELECT t.party_meeting_type_id AS id, t.type AS name,
                   t.party_meeting_main_type_id AS mainTypeId,
                   COUNT(m.party_meeting_id) AS meetings
            FROM party_meeting_type t
            LEFT JOIN party_meeting m ON m.party_meeting_type_id = t.party_meeting_type_id
                 AND m.start_date >= :floor
            GROUP BY t.party_meeting_type_id, t.type, t.party_meeting_main_type_id
            HAVING meetings > 0 ORDER BY meetings DESC
        """), {"floor": _DATE_FLOOR}).mappings().all()
        levels = self.db.execute(text("""
            SELECT l.party_meeting_level_id AS id, l.level AS name,
                   COUNT(m.party_meeting_id) AS meetings
            FROM party_meeting_level l
            LEFT JOIN party_meeting m ON m.party_meeting_level_id = l.party_meeting_level_id
                 AND m.start_date >= :floor
            GROUP BY l.party_meeting_level_id, l.level ORDER BY l.order_no
        """), {"floor": _DATE_FLOOR}).mappings().all()
        occurrences = self.db.execute(text(
            "SELECT party_meeting_occurrence_id AS id, occurrence AS name FROM party_meeting_occurrence ORDER BY order_no"
        )).mappings().all()
        span = self.db.execute(text(
            "SELECT MIN(start_date) AS dmin, MAX(start_date) AS dmax FROM party_meeting "
            "WHERE start_date >= :floor AND start_date <= (CURDATE() + INTERVAL 1 YEAR)"
        ), {"floor": _DATE_FLOOR}).mappings().first()
        return {
            "mainTypes": [dict(r) for r in main_types],
            "types": [dict(r) for r in types],
            "levels": [dict(r) for r in levels],
            "occurrences": [dict(r) for r in occurrences],
            "dateMin": str(span["dmin"]) if span and span["dmin"] else None,
            "dateMax": str(span["dmax"]) if span and span["dmax"] else None,
        }

    # ---- KPIs + chart aggregates ----
    def overview(self, filters):
        where, params = self._where(filters)
        base = _BASE + where

        summary = self.db.execute(text(f"""
            SELECT COUNT(*)                          AS totalMeetings,
                   SUM(m.is_conducted = 'Y')         AS conducted,
                   SUM(m.is_conducted = 'N')         AS notConducted,
                   SUM(m.is_conducted IS NULL)       AS pending,
                   SUM(m.is_conducted_by_ivr = 'Y')  AS viaIvr,
                   COUNT(DISTINCT m.party_meeting_type_id)  AS typeCount,
                   COUNT(DISTINCT m.party_meeting_level_id) AS levelCount
            {base}"""), params).mappings().first()

        by_month = self.db.execute(text(f"""
            SELECT DATE_FORMAT(m.start_date, '%Y-%m') AS label,
                   COUNT(*) AS value,
                   SUM(m.is_conducted = 'Y') AS conducted
            {base}
            GROUP BY label ORDER BY label"""), params).mappings().all()

        by_type = self.db.execute(text(f"""
            SELECT COALESCE(t.type, 'Unknown') AS label, COUNT(*) AS value,
                   SUM(m.is_conducted = 'Y') AS conducted
            {base}
            GROUP BY m.party_meeting_type_id, t.type ORDER BY value DESC LIMIT 12"""), params).mappings().all()

        by_level = self.db.execute(text(f"""
            SELECT COALESCE(l.level, 'Unknown') AS label, COUNT(*) AS value,
                   SUM(m.is_conducted = 'Y') AS conducted
            {base}
            GROUP BY m.party_meeting_level_id, l.level ORDER BY value DESC"""), params).mappings().all()

        by_main = self.db.execute(text(f"""
            SELECT COALESCE(mt.meeting_type, 'Other') AS label, COUNT(*) AS value
            {base}
            GROUP BY mt.party_meeting_main_type_id, mt.meeting_type ORDER BY value DESC"""), params).mappings().all()

        by_occurrence = self.db.execute(text(f"""
            SELECT COALESCE(o.occurrence, 'Unknown') AS label, COUNT(*) AS value
            {base}
            GROUP BY m.party_meeting_occurrence_id, o.occurrence ORDER BY value DESC"""), params).mappings().all()

        # Supporting totals — child tables are small and historical; drive from the
        # child and join up to the filtered meeting set (cheap PK joins).
        supporting = {
            "invitees": self._child_total("party_meeting_invitee", filters),
            "attendance": self._child_total("party_meeting_attendance", filters),
            "minutes": self._child_total("party_meeting_minute", filters, extra="AND (c.is_deleted IS NULL OR c.is_deleted <> 'Y')"),
            "actionPoints": self._child_total("party_meeting_minute", filters, extra="AND c.is_actionable = 'Y'"),
            "documents": self._child_total("party_meeting_document", filters, extra="AND (c.is_deleted IS NULL OR c.is_deleted NOT IN ('Y','1'))"),
        }

        s = dict(summary or {})
        for k in ("totalMeetings", "conducted", "notConducted", "pending", "viaIvr", "typeCount", "levelCount"):
            s[k] = int(s.get(k) or 0)
        s["conductedPct"] = round(s["conducted"] / s["totalMeetings"] * 100, 1) if s["totalMeetings"] else 0.0
        s.update(supporting)

        return {
            "summary": s,
            "byMonth": [self._intval(r) for r in by_month],
            "byType": [self._intval(r) for r in by_type],
            "byLevel": [self._intval(r) for r in by_level],
            "byMainType": [self._intval(r) for r in by_main],
            "byOccurrence": [self._intval(r) for r in by_occurrence],
        }

    def _child_total(self, child_table, filters, extra=""):
        where, params = self._where(filters)
        sql = f"""
            SELECT COUNT(*) AS n
            FROM {child_table} c
            JOIN party_meeting m ON m.party_meeting_id = c.party_meeting_id
            LEFT JOIN party_meeting_type t ON t.party_meeting_type_id = m.party_meeting_type_id
            {where} {extra}"""
        return int(self.db.execute(text(sql), params).scalar() or 0)

    @staticmethod
    def _intval(r):
        d = dict(r)
        for k in ("value", "conducted"):
            if k in d and d[k] is not None:
                d[k] = int(d[k])
        return d

    # ---- meetings listing ----
    def meetings(self, filters, limit=50, offset=0, sort="recent"):
        where, params = self._where(filters)
        total = self.db.execute(text(f"SELECT COUNT(*) {_BASE} {where}"), params).scalar() or 0
        order = {
            "recent": "m.start_date DESC, m.party_meeting_id DESC",
            "oldest": "m.start_date ASC",
            "name": "m.meeting_name ASC",
        }.get(sort, "m.start_date DESC, m.party_meeting_id DESC")
        params = {**params, "limit": int(limit), "offset": int(offset)}
        rows = self.db.execute(text(f"""
            SELECT m.party_meeting_id AS id, m.meeting_name AS name,
                   t.type AS type, mt.meeting_type AS mainType,
                   l.level AS level, o.occurrence AS occurrence,
                   m.start_date AS startDate, m.end_date AS endDate,
                   m.is_conducted AS conducted, m.is_conducted_by_ivr AS ivr,
                   m.conducted_date AS conductedDate, m.location_value AS locationValue,
                   (SELECT COUNT(*) FROM party_meeting_invitee i WHERE i.party_meeting_id = m.party_meeting_id) AS invitees,
                   (SELECT COUNT(*) FROM party_meeting_attendance a WHERE a.party_meeting_id = m.party_meeting_id) AS attendance,
                   (SELECT COUNT(*) FROM party_meeting_minute mn WHERE mn.party_meeting_id = m.party_meeting_id
                        AND (mn.is_deleted IS NULL OR mn.is_deleted <> 'Y')) AS minutes,
                   (SELECT COUNT(*) FROM party_meeting_document d WHERE d.party_meeting_id = m.party_meeting_id
                        AND (d.is_deleted IS NULL OR d.is_deleted NOT IN ('Y','1'))) AS documents
            {_BASE} {where}
            ORDER BY {order} LIMIT :limit OFFSET :offset"""), params).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["startDate"] = str(d["startDate"]) if d.get("startDate") else None
            d["endDate"] = str(d["endDate"]) if d.get("endDate") else None
            d["conductedDate"] = str(d["conductedDate"]) if d.get("conductedDate") else None
            for k in ("invitees", "attendance", "minutes", "documents"):
                d[k] = int(d.get(k) or 0)
            out.append(d)
        return {"total": int(total), "rows": out, "limit": int(limit), "offset": int(offset)}
