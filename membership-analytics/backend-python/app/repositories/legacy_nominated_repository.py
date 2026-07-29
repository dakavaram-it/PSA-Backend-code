"""
dakavara_pa legacy tables for Nominated Posts:
departments, board, position, board_level, nominated_post_position,
nominated_post_member, nominated_post, nomination_post_candidate, tdp_cadre,
nominated_post_application, nominated_post_final, nominated_post_govt_order,
govt_order, govt_order_documents, application_document, nominated_post_refer_details.
"""

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


class LegacyNominatedRepository:
    def __init__(self, dakavara_db: Session):
        self.db = dakavara_db

    @staticmethod
    def _rows(rows):
        return [dict(r) for r in rows]

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def resolve_candidate_links(
        self,
        nomination_post_candidate_id: int | None,
        nominated_post_member_id: int | None = None,
        enrollment_id: int | None = None,
    ) -> dict:
        """
        Resolve legacy nominated_post_id and nominated_post_application_id
        from dakavara_pa when the client did not send them.
        """
        if not nomination_post_candidate_id:
            return {}

        params = {
            "npc_id": nomination_post_candidate_id,
            "member_id": nominated_post_member_id,
            "enrollment_id": enrollment_id,
        }
        member_clause = (
            "AND NPA.nominated_post_member_id = :member_id"
            if nominated_post_member_id
            else ""
        )
        enrollment_clause = (
            "AND NPA.enrollment_id = :enrollment_id"
            if enrollment_id
            else ""
        )

        app = self._row(
            self.db.execute(
                text(f"""
                    SELECT
                        NPA.nominated_post_application_id AS nominatedPostApplicationId,
                        NPA.application_status_id AS applicationStatusId,
                        NPA.nominated_post_member_id AS nominatedPostMemberId
                    FROM nominated_post_application NPA
                    WHERE NPA.nomination_post_candidate_id = :npc_id
                      AND NPA.is_deleted = 'N'
                      {member_clause}
                      {enrollment_clause}
                    ORDER BY NPA.nominated_post_application_id DESC
                    LIMIT 1
                """),
                params,
            ).mappings().first()
        )

        np = self._row(
            self.db.execute(
                text("""
                    SELECT
                        NP.nominated_post_id AS nominatedPostId,
                        NP.nominated_post_status_id AS nominatedPostStatusId,
                        NPS.status AS legacyPostStatus
                    FROM nominated_post NP
                    LEFT JOIN nominated_post_status NPS
                      ON NP.nominated_post_status_id = NPS.nominated_post_status_id
                    WHERE NP.nomination_post_candidate_id = :npc_id
                      AND NP.is_deleted = 'N'
                      AND (:member_id IS NULL OR NP.nominated_post_member_id = :member_id)
                    ORDER BY NP.nominated_post_id DESC
                    LIMIT 1
                """),
                params,
            ).mappings().first()
        )

        out = {}
        if app:
            out.update(app)
        if np:
            out.update(np)
        return out

    def has_active_final_assignment(
        self,
        tdp_cadre_id: int,
        enrollment_id: int | None = None,
    ) -> bool:
        enrollment_clause = (
            "AND NPF.enrollment_id = :enrollment_id"
            if enrollment_id
            else ""
        )
        row = self.db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM nominated_post_final NPF
                JOIN nomination_post_candidate NPC
                  ON NPF.nomination_post_candidate_id = NPC.nomination_post_candidate_id
                 AND NPC.is_deleted = 'N'
                WHERE NPC.tdp_cadre_id = :tdp_cadre_id
                  AND NPF.is_deleted = 'N'
                  AND COALESCE(NPF.is_expired, 'N') = 'N'
                  {enrollment_clause}
            """),
            {"tdp_cadre_id": tdp_cadre_id, "enrollment_id": enrollment_id},
        ).mappings().first()
        return int(row["cnt"] or 0) > 0

    def get_documents_for_candidates(
        self,
        nomination_post_candidate_ids: list[int],
    ) -> dict[int, list[dict]]:
        if not nomination_post_candidate_ids:
            return {}
        sql = text("""
            SELECT
                AD.nomination_post_candidate_id AS nominationPostCandidateId,
                AD.application_document_id AS applicationDocumentId,
                AD.nominated_post_application_id AS nominatedPostApplicationId,
                AD.file_path AS filePath,
                AD.inserted_date AS insertedDate
            FROM application_document AD
            WHERE AD.nomination_post_candidate_id IN :npc_ids
              AND AD.is_deleted = 'N'
            ORDER BY AD.application_document_id DESC
        """).bindparams(bindparam("npc_ids", expanding=True))
        rows = self._rows(
            self.db.execute(sql, {"npc_ids": nomination_post_candidate_ids}).mappings().all()
        )
        grouped: dict[int, list] = {}
        for row in rows:
            npc_id = int(row["nominationPostCandidateId"])
            grouped.setdefault(npc_id, []).append(row)
        return grouped

    def get_referrals_for_candidates(
        self,
        nomination_post_candidate_ids: list[int],
    ) -> dict[int, list[dict]]:
        if not nomination_post_candidate_ids:
            return {}
        sql = text("""
            SELECT
                RD.nomination_post_candidate_id AS nominationPostCandidateId,
                RD.nominated_post_refer_details_id AS nominatedPostReferDetailsId,
                RD.nominated_post_application_id AS nominatedPostApplicationId,
                RD.refer_cadre_id AS referCadreId,
                TRIM(CONCAT(COALESCE(CR.first_name,''),' ',COALESCE(CR.last_name,''))) AS referCadreName,
                CR.membership_id AS referMembershipId
            FROM nominated_post_refer_details RD
            LEFT JOIN tdp_cadre CR ON RD.refer_cadre_id = CR.tdp_cadre_id AND CR.is_deleted = 'N'
            WHERE RD.nomination_post_candidate_id IN :npc_ids
              AND RD.is_deleted = 'N'
            ORDER BY RD.nominated_post_refer_details_id DESC
        """).bindparams(bindparam("npc_ids", expanding=True))
        rows = self._rows(
            self.db.execute(sql, {"npc_ids": nomination_post_candidate_ids}).mappings().all()
        )
        grouped: dict[int, list] = {}
        for row in rows:
            npc_id = int(row["nominationPostCandidateId"])
            grouped.setdefault(npc_id, []).append(row)
        return grouped

    def get_govt_orders_for_candidates(
        self,
        nomination_post_candidate_ids: list[int],
    ) -> dict[int, list[dict]]:
        if not nomination_post_candidate_ids:
            return {}
        sql = text("""
            SELECT
                NPGO.nomination_post_candidate_id AS nominationPostCandidateId,
                NPGO.nominated_post_govt_order_id AS nominatedPostGovtOrderId,
                NPGO.nominated_post_id AS nominatedPostId,
                NPGO.nominated_post_application_id AS nominatedPostApplicationId,
                GO.govt_order_id AS govtOrderId,
                GO.order_name AS orderName,
                GO.order_code AS orderCode,
                GO.from_date AS fromDate,
                GO.to_date AS toDate,
                GO.remarks AS goRemarks
            FROM nominated_post_govt_order NPGO
            JOIN govt_order GO ON NPGO.govt_order_id = GO.govt_order_id AND GO.is_deleted = 'N'
            WHERE NPGO.nomination_post_candidate_id IN :npc_ids
              AND NPGO.is_deleted = 'N'
            ORDER BY NPGO.nominated_post_govt_order_id DESC
        """).bindparams(bindparam("npc_ids", expanding=True))
        order_rows = self._rows(
            self.db.execute(sql, {"npc_ids": nomination_post_candidate_ids}).mappings().all()
        )
        if not order_rows:
            return {}

        govt_order_ids = list({int(r["govtOrderId"]) for r in order_rows})
        doc_sql = text("""
            SELECT
                GOD.govt_order_id AS govtOrderId,
                GOD.govt_order_documents_id AS govtOrderDocumentsId,
                GOD.path AS documentPath
            FROM govt_order_documents GOD
            WHERE GOD.govt_order_id IN :go_ids
              AND GOD.is_deleted = 'N'
            ORDER BY GOD.govt_order_documents_id DESC
        """).bindparams(bindparam("go_ids", expanding=True))
        doc_rows = self._rows(
            self.db.execute(doc_sql, {"go_ids": govt_order_ids}).mappings().all()
        )
        docs_by_go: dict[int, list] = {}
        for doc in doc_rows:
            go_id = int(doc["govtOrderId"])
            docs_by_go.setdefault(go_id, []).append(doc)

        grouped: dict[int, list] = {}
        for row in order_rows:
            npc_id = int(row["nominationPostCandidateId"])
            go_id = int(row["govtOrderId"])
            entry = dict(row)
            entry["documents"] = docs_by_go.get(go_id, [])
            grouped.setdefault(npc_id, []).append(entry)
        return grouped

    def get_final_status_for_candidates(
        self,
        nomination_post_candidate_ids: list[int],
    ) -> dict[int, dict]:
        if not nomination_post_candidate_ids:
            return {}
        sql = text("""
            SELECT
                NPF.nomination_post_candidate_id AS nominationPostCandidateId,
                NPF.nominated_post_final_id AS nominatedPostFinalId,
                NPF.nominated_post_application_id AS nominatedPostApplicationId,
                NPF.nominated_post_id AS nominatedPostId,
                NPF.application_status_id AS applicationStatusId,
                NPF.is_prefered AS isPreferred,
                NPF.is_expired AS isExpired
            FROM nominated_post_final NPF
            WHERE NPF.nomination_post_candidate_id IN :npc_ids
              AND NPF.is_deleted = 'N'
            ORDER BY NPF.nominated_post_final_id DESC
        """).bindparams(bindparam("npc_ids", expanding=True))
        rows = self.db.execute(sql, {"npc_ids": nomination_post_candidate_ids}).mappings().all()
        result: dict[int, dict] = {}
        for row in rows:
            npc_id = int(row["nominationPostCandidateId"])
            if npc_id not in result:
                result[npc_id] = dict(row)
        return result

    def build_legacy_context_batch(
        self,
        nomination_post_candidate_ids: list[int],
    ) -> dict[int, dict]:
        """Aggregate application documents, referrals, GO, and final records per NPC id."""
        npc_ids = [int(i) for i in nomination_post_candidate_ids if i]
        if not npc_ids:
            return {}

        documents = self.get_documents_for_candidates(npc_ids)
        referrals = self.get_referrals_for_candidates(npc_ids)
        govt_orders = self.get_govt_orders_for_candidates(npc_ids)
        finals = self.get_final_status_for_candidates(npc_ids)

        context: dict[int, dict] = {}
        for npc_id in npc_ids:
            final = finals.get(npc_id)
            context[npc_id] = {
                "isFinalized": final is not None and final.get("isExpired") != "Y",
                "final": final,
                "documents": documents.get(npc_id, []),
                "referrals": referrals.get(npc_id, []),
                "govtOrders": govt_orders.get(npc_id, []),
            }
        return context

    def get_existing_attached_candidates(self, nominated_post_member_id: int) -> list[dict]:
        sql = text("""
            SELECT
                NP.nominated_post_id AS nominatedPostId,
                NPC.nomination_post_candidate_id AS nominationPostCandidateId,
                NPC.tdp_cadre_id AS tdpCadreId,
                CR.membership_id AS membershipId,
                NPC.candidate_name AS candidateName,
                NPC.mobile_no AS mobileNo,
                NPC.gender AS gender,
                NPC.age AS age,
                CR.date_of_birth AS dob,
                NPS.status AS legacyPostStatus,
                NPA.nominated_post_application_id AS nominatedPostApplicationId,
                NPA.application_status_id AS applicationStatusId,
                NPF.nominated_post_final_id AS nominatedPostFinalId,
                NPF.is_expired AS finalIsExpired,
                GO.order_name AS goOrderName,
                GO.order_code AS goOrderCode
            FROM nominated_post NP
            JOIN nominated_post_status NPS
              ON NP.nominated_post_status_id = NPS.nominated_post_status_id
            LEFT JOIN nomination_post_candidate NPC
              ON NP.nomination_post_candidate_id = NPC.nomination_post_candidate_id
             AND NPC.is_deleted = 'N'
            LEFT JOIN tdp_cadre CR
              ON NPC.tdp_cadre_id = CR.tdp_cadre_id AND CR.is_deleted = 'N'
            LEFT JOIN nominated_post_application NPA
              ON NPA.nomination_post_candidate_id = NPC.nomination_post_candidate_id
             AND NPA.nominated_post_member_id = NP.nominated_post_member_id
             AND NPA.is_deleted = 'N'
            LEFT JOIN nominated_post_final NPF
              ON NPF.nomination_post_candidate_id = NPC.nomination_post_candidate_id
             AND NPF.nominated_post_member_id = NP.nominated_post_member_id
             AND NPF.is_deleted = 'N'
            LEFT JOIN nominated_post_govt_order NPGO
              ON NPGO.nomination_post_candidate_id = NPC.nomination_post_candidate_id
             AND NPGO.nominated_post_id = NP.nominated_post_id
             AND NPGO.is_deleted = 'N'
            LEFT JOIN govt_order GO
              ON NPGO.govt_order_id = GO.govt_order_id AND GO.is_deleted = 'N'
            WHERE NP.nominated_post_member_id = :nominated_post_member_id
              AND NP.is_deleted = 'N'
              AND NP.nomination_post_candidate_id IS NOT NULL
            ORDER BY NP.nominated_post_id DESC
        """)
        return self._rows(
            self.db.execute(sql, {"nominated_post_member_id": nominated_post_member_id}).mappings().all()
        )
