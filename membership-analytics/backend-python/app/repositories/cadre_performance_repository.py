from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.utils.cadre_photo import build_cadre_photo_url, build_document_list, normalize_mid
from app.utils.db_serialize import attach_photo_url, serialize_db_row


# Leader-feedback questions surfaced in the compare view (members_track.question
# ids). leader_feedback stores one q{n}_option / q{n}_points pair per id.
FEEDBACK_QUESTION_IDS = (16, 17, 18, 19, 20, 21, 24)

# Question names rarely change — cache them process-wide after the first read.
_feedback_question_cache: dict[int, str] | None = None


def _pick(row: dict, *keys, default=None):
    if not row:
        return default
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
        lk = key.lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return default


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class CadrePerformanceRepository:
    def __init__(self, report_ratings_db: Session):
        self.db = report_ratings_db
        self._image_base = get_settings().cadre_image_base_url
        self._documents_base = get_settings().nominated_post_documents_base_url

    @staticmethod
    def _rows(rows):
        return [dict(r) for r in rows]

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    @staticmethod
    def _mids_csv(mids: list[str]) -> str:
        cleaned = []
        seen = set()
        for mid in mids:
            m = normalize_mid(mid)
            if m and m not in seen:
                seen.add(m)
                cleaned.append(m)
        return ",".join(cleaned)

    def _call_procedure(self, procedure_name: str, mids_csv: str):
        if not mids_csv:
            return
        sa_conn = self.db.connection()
        dbapi_conn = sa_conn.connection
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"CALL {procedure_name}(%s)", (mids_csv,))
            while cursor.nextset():
                pass
        finally:
            cursor.close()
        self.db.commit()

    def cadre_performance_update(self, mids: list[str]):
        mids_csv = self._mids_csv(mids)
        if not mids_csv:
            raise ValueError("At least one MID is required")
        self._call_procedure("cadre_performance_update", mids_csv)

    def cadre_performance_report(self, mids: list[str]):
        mids_csv = self._mids_csv(mids)
        if not mids_csv:
            raise ValueError("At least one MID is required")
        self._call_procedure("cadre_performance_report", mids_csv)

    def get_cadre_details_by_mids(self, mids: list[str]):
        cleaned = [normalize_mid(m) for m in mids if normalize_mid(m)]
        if not cleaned:
            return []
        placeholders = ", ".join([f":mid_{i}" for i in range(len(cleaned))])
        params = {f"mid_{i}": mid for i, mid in enumerate(cleaned)}
        rows = self.db.execute(text(f"""
            SELECT *
            FROM cadre_details
            WHERE membership_id IN ({placeholders})
        """), params).mappings().all()
        return self._rows(rows)

    def get_cadre_details_by_mid(self, mid: str):
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            return None
        row = self.db.execute(text("""
            SELECT * FROM cadre_details WHERE membership_id = :mid LIMIT 1
        """), {"mid": mid_clean}).mappings().first()
        return self._row(row)

    def get_performance_reports_by_mids(self, mids: list[str]):
        cleaned = [normalize_mid(m) for m in mids if normalize_mid(m)]
        if not cleaned:
            return []
        placeholders = ", ".join([f":mid_{i}" for i in range(len(cleaned))])
        params = {f"mid_{i}": mid for i, mid in enumerate(cleaned)}
        rows = self.db.execute(text(f"""
            SELECT * FROM cadre_performace_report
            WHERE `MID` IN ({placeholders})
        """), params).mappings().all()
        return self._rows(rows)

    def list_cadre_details_with_score(self, limit: int = 500):
        """Candidate-pool listing: cadre_details_nom rows joined to their materialized
        PERFORMANCE SCORE (cadre_performace_report_nom), highest score first.

        Uses the nominated-post pool tables (`*_nom`), which carry the same profile
        columns as cadre_details plus the candidate's previously-applied application
        (board_name / department_name / position_name / app_status).

        Read-only, lookup-only — does NOT run the performance procedures. Rows with
        no report yet sort last (NULL score). The per-category point breakdown is
        loaded separately via get_performance_reports_by_mids_nom on the returned MIDs.
        """
        rows = self.db.execute(text("""
            SELECT
                cd.membership_id AS membership_id,
                cd.cadre_id      AS cadre_id,
                cd.MEMBERNAME    AS name,
                cd.AGE           AS age,
                cd.GENDER        AS gender,
                cd.MOBILENO      AS mobile,
                cd.PARLIAMENT    AS parliament,
                cd.ASSEMBLY      AS assembly,
                cd.REGION_NAME   AS mandal,
                cd.VILLAGE       AS village,
                cd.CASTE         AS caste,
                cd.CATEGORY      AS category,
                cd.IMAGE         AS image,
                cd.board_name       AS board_name,
                cd.department_name  AS department_name,
                cd.position_name    AS position_name,
                cd.app_status       AS app_status,
                cd.npa_updated_time AS npa_updated_time,
                cd.documents        AS documents,
                pr.`PERFORMANCE SCORE` AS score
            FROM cadre_details_nom cd
            LEFT JOIN cadre_performace_report_nom pr
                ON pr.`MID` COLLATE utf8mb4_general_ci = cd.membership_id COLLATE utf8mb4_general_ci
            WHERE cd.enrollment_id = 1
            ORDER BY (pr.`PERFORMANCE SCORE` IS NULL) ASC, pr.`PERFORMANCE SCORE` DESC
            LIMIT :limit
        """), {"limit": limit}).mappings().all()
        return self._rows(rows)

    def get_cadre_details_nom_by_mids(self, mids: list[str]):
        """Candidate-pool profile rows from cadre_details_nom (the nominated-post pool
        snapshot). Same columns as cadre_details plus previous_role/present_role/documents
        + the applied-application fields. Used by the pool compare (no procedure runs)."""
        cleaned = [normalize_mid(m) for m in mids if normalize_mid(m)]
        if not cleaned:
            return []
        placeholders = ", ".join([f":mid_{i}" for i in range(len(cleaned))])
        params = {f"mid_{i}": mid for i, mid in enumerate(cleaned)}
        rows = self.db.execute(text(f"""
            SELECT *
            FROM cadre_details_nom
            WHERE membership_id IN ({placeholders})
        """), params).mappings().all()
        return self._rows(rows)

    def get_performance_reports_by_mids_nom(self, mids: list[str]):
        """Candidate-pool per-category point breakdown from cadre_performace_report_nom
        (the nominated-post pool report). Same columns as get_performance_reports_by_mids,
        different source table — the pool ranks on the *_nom snapshot."""
        cleaned = [normalize_mid(m) for m in mids if normalize_mid(m)]
        if not cleaned:
            return []
        placeholders = ", ".join([f":mid_{i}" for i in range(len(cleaned))])
        params = {f"mid_{i}": mid for i, mid in enumerate(cleaned)}
        rows = self.db.execute(text(f"""
            SELECT * FROM cadre_performace_report_nom
            WHERE `MID` IN ({placeholders})
        """), params).mappings().all()
        return self._rows(rows)

    def map_cadre_details_to_profile(self, row: dict | None) -> dict | None:
        if not row:
            return None
        image = _pick(row, "IMAGE", "image")
        return {
            "membershipId": _pick(row, "membership_id", "membershipId"),
            "membershipNo": None,
            "mid": f"#{normalize_mid(_pick(row, 'membership_id', 'membershipId'))}",
            "tdpCadreId": _int(_pick(row, "cadre_id", "cadreId")),
            "candidateName": _pick(row, "MEMBERNAME", "memberName", "candidateName"),
            "mobileNo": _pick(row, "MOBILENO", "mobileNo"),
            "gender": _pick(row, "GENDER", "gender"),
            "age": _int(_pick(row, "AGE", "age")),
            "dob": None,
            "occupation": None,
            "renewalTimes": 1 if str(_pick(row, "ISRENEWAL", "isRenewal", default="")).upper() == "Y" else 0,
            "mandal": _pick(row, "REGION_NAME", "regionName", "mandal"),
            "village": _pick(row, "VILLAGE", "village"),
            "assembly": _pick(row, "ASSEMBLY", "assembly"),
            "parliament": _pick(row, "PARLIAMENT", "parliament"),
            "casteStateId": None,
            "casteName": _pick(row, "CASTE", "caste", "casteName"),
            "castCategory": _pick(row, "CATEGORY", "category", "castCategory"),
            "designation": None,
            "constituencyPercent": None,
            "castePercentage": _num(_pick(row, "caste_percentage", "castePercentage")),
            "photo": image,
            "photoUrl": build_cadre_photo_url(image, self._image_base),
        }

    def map_performance_report(self, row: dict | None) -> dict | None:
        if not row:
            return None
        membership_id = normalize_mid(_pick(row, "#MID", "MID", "membership_id", "membershipId"))
        mapped = {
            "membershipId": membership_id,
            "performanceScore": _num(_pick(row, "PERFORMANCE SCORE", "performanceScore")),
            "pedalaSevalo": _int(_pick(row, "PEDALA SEVALO", "pedalaSevalo")),
            "pedalaSevaloPoints": _num(_pick(row, "POINTS (Pedala Sevalo)", "pedalaSevaloPoints")),
            "firstMembershipYear": _pick(row, "YEAR", "firstMembershipYear"),
            "firstMembershipPoints": _num(_pick(row, "POINTS (1st Membership)", "firstMembershipPoints")),
            "renewalTimes": _int(_pick(row, "NO OF TIME", "renewalTimes")),
            "renewalPoints": _num(_pick(row, "POINTS (No of Times)", "renewalPoints")),
            "referrals": _int(_pick(row, "REGS", "referrals")),
            "referralPoints": _num(_pick(row, "POINTS (Referrals)", "referralPoints")),
            "mandalVoteSharePercent": _num(_pick(row, "MANDAL/TOWN 7.5%", "mandalVoteSharePercent")),
            "mandalVoteSharePoints": _num(_pick(row, "POINTS (Mandal Vote Share)", "mandalVoteSharePoints")),
            "boothVoteSharePercent": _num(_pick(row, "BOOTH 7.5%", "boothVoteSharePercent")),
            "boothVoteSharePoints": _num(_pick(row, "POINTS (Booth Vote Share)", "boothVoteSharePoints")),
            "mandalMembershipActual": _int(_pick(row, "MANDAL/TOWN ACTUAL", "mandalMembershipActual")),
            "mandalMembershipAch": _int(_pick(row, "ACH (Mandal Membership)", "mandalMembershipAch")),
            "mandalMembershipAchPercent": _num(_pick(row, "ACH % (Mandal Membership)", "mandalMembershipAchPercent")),
            "mandalMembershipPoints": _num(_pick(row, "MANDAL/TOWN 5% POINTS", "mandalMembershipPoints")),
            "boothMembershipActual": _int(_pick(row, "BOOTH ACTUAL", "boothMembershipActual")),
            "boothMembershipAch": _int(_pick(row, "ACH (Booth Membership)", "boothMembershipAch")),
            "boothMembershipAchPercent": _num(_pick(row, "ACH % (Booth Membership)", "boothMembershipAchPercent")),
            "boothMembershipPoints": _num(_pick(row, "BOOTH 5% POINTS", "boothMembershipPoints")),
            "mandalD2dActual": _int(_pick(row, "MANDAL/TOWN ACT", "mandalD2dActual")),
            "mandalD2dAch": _int(_pick(row, "ACH (Mandal D2D)", "mandalD2dAch")),
            "mandalD2dAchPercent": _num(_pick(row, "ACH % (Mandal D2D)", "mandalD2dAchPercent")),
            "mandalD2dPoints": _num(_pick(row, "MANDAL/TOWN 15%", "mandalD2dPoints")),
            "boothD2dActual": _int(_pick(row, "BOOTH ACT", "boothD2dActual")),
            "boothD2dAch": _int(_pick(row, "ACH (Booth D2D)", "boothD2dAch")),
            "boothD2dAchPercent": _num(_pick(row, "ACH % (Booth D2D)", "boothD2dAchPercent")),
            "boothD2dPoints": _num(_pick(row, "BOOTH 15%", "boothD2dPoints")),
            "positions2018_2020": _pick(row, "2018 - 2020", "positions2018_2020"),
            "positions2016_2018": _pick(row, "2016 - 2018", "positions2016_2018"),
            "positions2014_2016": _pick(row, "2014 - 2016", "positions2014_2016"),
            "positionsPoints": _num(_pick(row, "POINTS (Positions)", "positionsPoints")),
        }
        mapped["raw"] = serialize_db_row(row)
        return mapped

    def serialize_cadre_details(self, row: dict | None) -> dict | None:
        data = attach_photo_url(row, self._image_base)
        if data is not None:
            data["documents"] = build_document_list(data.get("documents"), self._documents_base)
        return data

    def get_feedback_questions(self) -> list[dict]:
        """Question id + name for the leader-feedback section, in display order.
        Names come from members_track.question (same RDS server) and are cached."""
        global _feedback_question_cache
        if _feedback_question_cache is None:
            ids = ", ".join(str(q) for q in FEEDBACK_QUESTION_IDS)
            rows = self.db.execute(text(f"""
                SELECT question_id, question_name
                FROM members_track.question
                WHERE question_id IN ({ids})
            """)).mappings().all()
            _feedback_question_cache = {
                _int(r["question_id"]): r["question_name"] for r in rows
            }
        return [
            {"questionId": qid, "questionName": _feedback_question_cache.get(qid)}
            for qid in FEEDBACK_QUESTION_IDS
        ]

    def get_leader_feedback_by_mids(self, mids: list[str]):
        cleaned = [normalize_mid(m) for m in mids if normalize_mid(m)]
        if not cleaned:
            return []
        placeholders = ", ".join([f":mid_{i}" for i in range(len(cleaned))])
        params = {f"mid_{i}": mid for i, mid in enumerate(cleaned)}
        cols = ["membership_id"]
        for q in FEEDBACK_QUESTION_IDS:
            cols.append(f"q{q}_option")
            cols.append(f"q{q}_points")
        cols.append("score")
        rows = self.db.execute(text(f"""
            SELECT {", ".join(cols)}
            FROM leader_feedback
            WHERE membership_id IN ({placeholders})
        """), params).mappings().all()
        return self._rows(rows)

    def map_leader_feedback(self, row: dict | None) -> dict | None:
        if not row:
            return None
        answers = {
            str(q): {
                "option": _pick(row, f"q{q}_option"),
                "points": _int(_pick(row, f"q{q}_points")),
            }
            for q in FEEDBACK_QUESTION_IDS
        }
        return {
            "membershipId": normalize_mid(_pick(row, "membership_id", "membershipId")),
            "score": _int(_pick(row, "score")),
            "answers": answers,
        }
