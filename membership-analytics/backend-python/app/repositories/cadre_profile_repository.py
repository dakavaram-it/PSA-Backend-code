from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.utils.cadre_photo import build_cadre_photo_url, normalize_mid

_PROFILE_SELECT = """
    SELECT
        CR.tdp_cadre_id AS tdpCadreId,
        CR.membership_id AS membershipId,
        CR.membership_no AS membershipNo,
        CONCAT('#', CR.membership_id) AS mid,
        TRIM(CONCAT(COALESCE(CR.first_name,''),' ',COALESCE(CR.last_name,''))) AS candidateName,
        CR.mobile_no AS mobileNo,
        CR.gender AS gender,
        CR.age AS age,
        CR.date_of_birth AS dob,
        OCC.occupation AS occupation,
        EQ.qualification AS education,
        CR.designation_name AS designation,
        CR.caste_state_id AS casteStateId,
        C.caste_name AS casteName,
        CCG.caste_category_group_name AS castCategory,
        COALESCE(CON.mandal, MT.tehsil_name) AS mandal,
        COALESCE(CON.address1, CON.address2) AS village,
        AC.name AS assembly,
        PC.name AS parliament,
        CR.image AS photo
    FROM tdp_cadre CR
    LEFT JOIN occupation OCC ON CR.occupation_id = OCC.occupation_id
    LEFT JOIN educational_qualifications EQ ON CR.education_id = EQ.educational_qualification_id
    LEFT JOIN caste_state CS ON CR.caste_state_id = CS.caste_state_id
    LEFT JOIN caste C ON CS.caste_id = C.caste_id
    LEFT JOIN caste_category_group CCG ON CS.caste_category_group_id = CCG.caste_category_group_id
    LEFT JOIN address CON ON CR.address_id = CON.address_id
    LEFT JOIN constituency CONS ON CR.constituency_id = CONS.constituency_id
    LEFT JOIN constituency AC ON CONS.assembly_constituency_id = AC.constituency_id
    LEFT JOIN constituency PC ON CONS.parliament_id = PC.constituency_id
    LEFT JOIN tehsil MT ON CONS.tehsil_id = MT.tehsil_id
"""


class CadreProfileRepository:
    def __init__(self, dakavara_db: Session, write_db: Session | None = None):
        self.dakavara_db = dakavara_db
        # Writable dakavara_pa session for the caste/sub-caste update only; falls
        # back to the read session when the UPDATE_DB_* credentials aren't set.
        self.write_db = write_db
        self._image_base = get_settings().cadre_image_base_url

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    @staticmethod
    def _rows(rows):
        return [dict(r) for r in rows]

    def _enrich_row(self, row: dict | None) -> dict | None:
        if not row:
            return None
        data = dict(row)
        if data.get("dob") is not None:
            data["dob"] = str(data["dob"])
        data["photoUrl"] = build_cadre_photo_url(data.get("photo"), self._image_base)
        data["constituencyPercent"] = None
        data["renewalTimes"] = None
        return data

    def get_profile_by_mid(self, mid: str):
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            return None
        sql = text(f"""
            {_PROFILE_SELECT}
            WHERE CR.is_deleted = 'N' AND CR.membership_id = :mid
            ORDER BY CR.tdp_cadre_id DESC
            LIMIT 1
        """)
        row = self.dakavara_db.execute(sql, {"mid": mid_clean}).mappings().first()
        return self._enrich_row(self._row(row))

    def get_profile_by_mobile(self, mobile: str):
        mobile_clean = mobile.strip()
        if not mobile_clean:
            return None
        sql = text(f"""
            {_PROFILE_SELECT}
            WHERE CR.is_deleted = 'N' AND CR.mobile_no = :mobile
            ORDER BY CR.tdp_cadre_id DESC
            LIMIT 1
        """)
        row = self.dakavara_db.execute(sql, {"mobile": mobile_clean}).mappings().first()
        return self._enrich_row(self._row(row))

    def get_profile_by_cadre_id(self, tdp_cadre_id: int):
        sql = text(f"""
            {_PROFILE_SELECT}
            WHERE CR.is_deleted = 'N' AND CR.tdp_cadre_id = :tdp_cadre_id
            LIMIT 1
        """)
        row = self.dakavara_db.execute(sql, {"tdp_cadre_id": tdp_cadre_id}).mappings().first()
        return self._enrich_row(self._row(row))

    def get_profiles_by_cadre_ids(self, tdp_cadre_ids) -> dict[int, dict]:
        """Batch profile lookup keyed by tdp_cadre_id (one query, no N+1)."""
        ids = sorted({int(i) for i in tdp_cadre_ids if i})
        if not ids:
            return {}
        placeholders = ", ".join(f":id_{idx}" for idx in range(len(ids)))
        params = {f"id_{idx}": v for idx, v in enumerate(ids)}
        sql = text(f"""
            {_PROFILE_SELECT}
            WHERE CR.is_deleted = 'N' AND CR.tdp_cadre_id IN ({placeholders})
        """)
        rows = self.dakavara_db.execute(sql, params).mappings().all()
        return {int(r["tdpCadreId"]): self._enrich_row(dict(r)) for r in rows}

    def get_caste_options(self, state_id: int = 1):
        """All castes for a state, each carrying its category group (OC/BC/...) and
        the ``caste_state_id`` to persist on tdp_cadre."""
        sql = text("""
            SELECT
                CC.caste_category_id AS casteCategoryGroupId,
                CC.category_name AS casteCategoryGroupName,
                CS.caste_state_id AS casteStateId,
                C.caste_name AS casteName
            FROM caste_state CS
            JOIN caste C ON CS.caste_id = C.caste_id
            JOIN caste_category_group CCG ON CS.caste_category_group_id = CCG.caste_category_group_id
            JOIN caste_category CC ON CC.caste_category_id = CCG.caste_category_id
            WHERE CS.state_id = :state_id
            ORDER BY CCG.caste_category_group_name, C.caste_name
        """)
        rows = self.dakavara_db.execute(sql, {"state_id": state_id}).mappings().all()
        return [dict(r) for r in rows]

    def get_occupation_options(self):
        """All occupations for the occupation edit dropdown."""
        sql = text("""
            SELECT occupation_id AS occupationId, occupation AS occupation
            FROM occupation
            ORDER BY occupation
        """)
        rows = self.dakavara_db.execute(sql).mappings().all()
        return [dict(r) for r in rows]

    def update_occupation(self, membership_id: str, occupation_id: int) -> int:
        """Set tdp_cadre.occupation_id for an active cadre by membership_id. Returns rows updated.

        Uses the dedicated writable session (UPDATE_DB_*) when configured; otherwise
        falls back to the read session, which will only succeed if that account has write access.
        """
        db = self.write_db or self.dakavara_db
        mid_clean = normalize_mid(membership_id)
        if not mid_clean:
            raise ValueError("Invalid membership ID")
        valid = db.execute(
            text("SELECT 1 FROM occupation WHERE occupation_id = :oid LIMIT 1"),
            {"oid": occupation_id},
        ).first()
        if not valid:
            raise ValueError(f"Invalid occupation_id: {occupation_id}")
        result = db.execute(
            text("""
                UPDATE tdp_cadre
                SET occupation_id = :oid
                WHERE membership_id = :mid AND is_deleted = 'N'
            """),
            {"oid": occupation_id, "mid": mid_clean},
        )
        db.commit()
        return result.rowcount

    def get_education_options(self):
        """All educational qualifications for the education edit dropdown."""
        sql = text("""
            SELECT educational_qualification_id AS educationId, qualification AS education
            FROM educational_qualifications
            ORDER BY qualification
        """)
        rows = self.dakavara_db.execute(sql).mappings().all()
        return [dict(r) for r in rows]

    def get_party_options(self):
        """Parties allowed when creating a manual (no-MID) nominated-post candidate."""
        sql = text("""
            SELECT party_id AS partyId, long_name AS longName, short_name AS shortName
            FROM party
            WHERE party_id IN (163, 872, 1892)
            ORDER BY short_name
        """)
        rows = self.dakavara_db.execute(sql).mappings().all()
        return [dict(r) for r in rows]

    def update_education(self, membership_id: str, education_id: int) -> int:
        """Set tdp_cadre.education_id for an active cadre by membership_id. Returns rows updated.

        Uses the dedicated writable session (UPDATE_DB_*) when configured; otherwise
        falls back to the read session, which will only succeed if that account has write access.
        """
        db = self.write_db or self.dakavara_db
        mid_clean = normalize_mid(membership_id)
        if not mid_clean:
            raise ValueError("Invalid membership ID")
        valid = db.execute(
            text("SELECT 1 FROM educational_qualifications WHERE educational_qualification_id = :eid LIMIT 1"),
            {"eid": education_id},
        ).first()
        if not valid:
            raise ValueError(f"Invalid education_id: {education_id}")
        result = db.execute(
            text("""
                UPDATE tdp_cadre
                SET education_id = :eid
                WHERE membership_id = :mid AND is_deleted = 'N'
            """),
            {"eid": education_id, "mid": mid_clean},
        )
        db.commit()
        return result.rowcount

    def update_caste_state(self, membership_id: str, caste_state_id: int) -> int:
        """Set tdp_cadre.caste_state_id for an active cadre by membership_id. Returns rows updated.

        Uses the dedicated writable session (UPDATE_DB_*) when configured; otherwise
        falls back to the read session, which will only succeed if that account has write access.
        """
        db = self.write_db or self.dakavara_db
        mid_clean = normalize_mid(membership_id)
        if not mid_clean:
            raise ValueError("Invalid membership ID")
        valid = db.execute(
            text("SELECT 1 FROM caste_state WHERE caste_state_id = :csid LIMIT 1"),
            {"csid": caste_state_id},
        ).first()
        if not valid:
            raise ValueError(f"Invalid caste_state_id: {caste_state_id}")
        result = db.execute(
            text("""
                UPDATE tdp_cadre
                SET caste_state_id = :csid
                WHERE membership_id = :mid AND is_deleted = 'N'
            """),
            {"csid": caste_state_id, "mid": mid_clean},
        )
        db.commit()
        return result.rowcount

    def search_profiles(self, mid: str | None = None, mobile: str | None = None, limit: int = 10):
        if mid:
            mid_clean = normalize_mid(mid)
            where_clause = "CR.membership_id = :value"
            value = mid_clean
        elif mobile:
            where_clause = "CR.mobile_no = :value"
            value = mobile.strip()
        else:
            raise ValueError("Either MID or mobile is required")

        sql = text(f"""
            {_PROFILE_SELECT}
            WHERE CR.is_deleted = 'N' AND {where_clause}
            ORDER BY CR.tdp_cadre_id DESC
            LIMIT :limit
        """)
        rows = self.dakavara_db.execute(sql, {"value": value, "limit": limit}).mappings().all()
        return [self._enrich_row(dict(r)) for r in rows]
