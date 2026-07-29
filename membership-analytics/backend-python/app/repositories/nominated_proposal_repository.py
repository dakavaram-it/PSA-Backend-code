import json
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.repositories.cadre_profile_repository import CadreProfileRepository
from app.repositories.legacy_nominated_repository import LegacyNominatedRepository

# Proposals in these statuses have completed (or ended) the workflow — no more candidates.
_CANDIDATE_ADD_BLOCKED_STATUSES = frozenset({"GO_ISSUED", "PUBLISHED", "REJECTED"})

# Manual-candidate party dropdown (dakavara_pa.party).
_MANUAL_CANDIDATE_PARTY_IDS = frozenset({163, 872, 1892})

# Workflow stages that still use physical (hard) delete — early mistakes before Feedback.
_HARD_DELETE_WORKFLOW_STAGES = frozenset({"ADD_PROFILES", "NEEDS_CORRECTION"})


class NominatedProposalRepository:
    def __init__(self, dakavara_db: Session, pa_track_db: Session):
        self.dakavara_db = dakavara_db
        self.pa_track_db = pa_track_db
        self.profile_repo = CadreProfileRepository(dakavara_db)
        self.legacy_repo = LegacyNominatedRepository(dakavara_db)

    @staticmethod
    def _rows(rows): return [dict(r) for r in rows]
    @staticmethod
    def _row(row): return dict(row) if row else None

    def get_status_id(self, status_code: str) -> int:
        row = self.pa_track_db.execute(text("""
            SELECT status_id FROM proposal_status_master
            WHERE status_code=:status_code AND is_active='Y' AND is_deleted='N'
        """), {"status_code": status_code}).mappings().first()
        if not row:
            raise ValueError(f"Status not configured: {status_code}")
        return int(row["status_id"])

    def generate_proposal_no(self):
        row = self.pa_track_db.execute(text("SELECT DATE_FORMAT(NOW(), '%Y%m%d%H%i%s') AS ts")).mappings().first()
        return f"NP-{row['ts']}"

    def get_capacity_from_legacy(self, enrollment_id, board_level_id, location_value, department_id, board_id, position_id, nominated_post_member_id):
        member_filter = "AND NPM.nominated_post_member_id=:nominated_post_member_id" if nominated_post_member_id else ""
        sql = text(f"""
            SELECT BL.level AS boardLevelName, NPM.nominated_post_member_id AS nominatedPostMemberId,
                   NPM.nominated_post_position_id AS nominatedPostPositionId, NPM.max_members AS maxMembers,
                   D.dept_name AS departmentName, B.board_name AS boardName, P.position_name AS positionName,
                   SUM(CASE WHEN LOWER(COALESCE(NPS.status,'')) IN ('confirmed','finalized','published','approved') THEN 1 ELSE 0 END) AS confirmedCount
            FROM nominated_post_member NPM
            JOIN board_level BL ON NPM.board_level_id=BL.board_level_id
            JOIN nominated_post_position NPP ON NPM.nominated_post_position_id=NPP.nominated_post_position_id AND NPP.is_deleted='N'
            JOIN departments D ON NPP.department_id=D.department_id
            JOIN board B ON NPP.board_id=B.board_id
            JOIN position P ON NPP.position_id=P.position_id
            LEFT JOIN nominated_post NP ON NPM.nominated_post_member_id=NP.nominated_post_member_id AND NP.is_deleted='N'
            LEFT JOIN nominated_post_status NPS ON NP.nominated_post_status_id=NPS.nominated_post_status_id
            WHERE NPM.is_deleted='N' AND NPM.enrollment_id=:enrollment_id
              AND NPM.board_level_id=:board_level_id AND NPM.location_value=:location_value
              AND D.department_id=:department_id AND B.board_id=:board_id AND P.position_id=:position_id
              {member_filter}
            GROUP BY BL.level,NPM.nominated_post_member_id,NPM.nominated_post_position_id,NPM.max_members,D.dept_name,B.board_name,P.position_name
            ORDER BY (NPM.max_members - SUM(CASE WHEN LOWER(COALESCE(NPS.status,'')) IN ('confirmed','finalized','published','approved') THEN 1 ELSE 0 END)) DESC
            LIMIT 1
        """)
        params = {
            "enrollment_id": enrollment_id, "board_level_id": board_level_id,
            "location_value": location_value, "department_id": department_id,
            "board_id": board_id, "position_id": position_id,
        }
        if nominated_post_member_id:
            params["nominated_post_member_id"] = nominated_post_member_id
        return self._row(self.dakavara_db.execute(sql, params).mappings().first())

    def count_active_proposed_candidates_for_member(self, nominated_post_member_id: int):
        row = self.pa_track_db.execute(text("""
            SELECT COUNT(c.proposal_candidate_id) AS proposedCount
            FROM nominated_post_proposal p
            JOIN nominated_post_proposal_candidate c ON p.proposal_id=c.proposal_id AND c.is_deleted='N'
            WHERE p.nominated_post_member_id=:nominated_post_member_id
              AND p.is_deleted='N' AND p.current_status_code NOT IN ('REJECTED','PUBLISHED')
        """), {"nominated_post_member_id": nominated_post_member_id}).mappings().first()
        return int(row["proposedCount"] or 0)

    def create_proposal(self, req, capacity):
        status_id = self.get_status_id("DRAFT")
        confirmed = int(capacity.get("confirmedCount") or 0)
        max_members = int(capacity.get("maxMembers") or 0)
        proposed = self.count_active_proposed_candidates_for_member(req.nominatedPostMemberId)
        open_count = max(max_members - confirmed - proposed, 0)
        # No open seats -> never create the proposal. Either the legacy fills already
        # confirm every seat, or an active proposal has candidates occupying them.
        if open_count <= 0:
            if confirmed >= max_members:
                raise ValueError("Position Already Confirmed in System")
            raise ValueError(
                f"Position Already Confirmed in System — all {max_members} seat(s) are taken "
                f"({confirmed} confirmed, {proposed} already proposed). Remove candidates from the "
                "existing proposal, or delete it, before creating a new one."
            )
        proposal_no = self.generate_proposal_no()

        self.pa_track_db.execute(text("""
            INSERT INTO nominated_post_proposal (
                proposal_no,enrollment_id,board_level_id,board_level_name,location_value,location_name,
                department_id,department_name,board_id,board_name,position_id,position_name,
                nominated_post_member_id,nominated_post_position_id,max_members,existing_confirmed_count,
                existing_open_count,current_status_id,current_status_code,remarks,created_by,created_by_name
            ) VALUES (
                :proposal_no,:enrollment_id,:board_level_id,:board_level_name,:location_value,:location_name,
                :department_id,:department_name,:board_id,:board_name,:position_id,:position_name,
                :nominated_post_member_id,:nominated_post_position_id,:max_members,:confirmed,
                :open_count,:status_id,'DRAFT',:remarks,:created_by,:created_by_name
            )
        """), {
            "proposal_no": proposal_no, "enrollment_id": req.enrollmentId,
            "board_level_id": req.boardLevelId, "board_level_name": capacity.get("boardLevelName"),
            "location_value": req.locationValue, "location_name": req.locationName,
            "department_id": req.departmentId, "department_name": capacity.get("departmentName"),
            "board_id": req.boardId, "board_name": capacity.get("boardName"),
            "position_id": req.positionId, "position_name": capacity.get("positionName"),
            "nominated_post_member_id": req.nominatedPostMemberId,
            "nominated_post_position_id": capacity.get("nominatedPostPositionId") or req.nominatedPostPositionId,
            "max_members": max_members, "confirmed": confirmed, "open_count": open_count,
            "status_id": status_id, "remarks": req.remarks,
            "created_by": req.createdBy, "created_by_name": req.createdByName,
        })
        proposal_id = self.pa_track_db.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().first()["id"]
        self.insert_audit(proposal_id,"CREATE_PROPOSAL",None,"DRAFT",req.createdBy,req.createdByName,"Proposal created",req.remarks)
        self.insert_event("NOMINATED_POST_PROPOSAL",proposal_id,"proposal.created",{"proposalId": proposal_id})
        self.pa_track_db.commit()
        return self.get_proposal_detail(proposal_id)

    def search_cadre(self, mid: str | None = None, mobile: str | None = None, limit: int = 10):
        return self.profile_repo.search_profiles(mid=mid, mobile=mobile, limit=limit)

    def get_candidate_snapshot_from_cadre(self, tdp_cadre_id: int):
        return self.profile_repo.get_profile_by_cadre_id(tdp_cadre_id)

    def get_candidate_snapshot_from_nomination_candidate(self, nomination_post_candidate_id: int):
        sql = text("""
            SELECT
                NPC.nomination_post_candidate_id AS nominationPostCandidateId,
                NPC.tdp_cadre_id AS tdpCadreId,
                CR.membership_id AS membershipId,
                COALESCE(NULLIF(TRIM(NPC.candidate_name), ''),
                    TRIM(CONCAT(COALESCE(CR.first_name,''),' ',COALESCE(CR.last_name,'')))) AS candidateName,
                COALESCE(NULLIF(TRIM(NPC.mobile_no), ''), CR.mobile_no) AS mobileNo,
                COALESCE(NULLIF(TRIM(NPC.gender), ''), CR.gender) AS gender,
                NPC.age AS age,
                NPC.caste_state_id AS casteStateId
            FROM nomination_post_candidate NPC
            LEFT JOIN tdp_cadre CR ON NPC.tdp_cadre_id = CR.tdp_cadre_id AND CR.is_deleted = 'N'
            WHERE NPC.nomination_post_candidate_id = :id AND NPC.is_deleted = 'N'
        """)
        return self._row(self.dakavara_db.execute(sql, {"id": nomination_post_candidate_id}).mappings().first())

    def _find_proposal_candidate(self, proposal_id, tdp_cadre_id):
        if not tdp_cadre_id:
            return None
        return self._row(self.pa_track_db.execute(text("""
            SELECT proposal_candidate_id, is_deleted
            FROM nominated_post_proposal_candidate
            WHERE proposal_id=:proposal_id AND tdp_cadre_id=:tdp_cadre_id
            ORDER BY proposal_candidate_id DESC
            LIMIT 1
        """), {"proposal_id": proposal_id, "tdp_cadre_id": tdp_cadre_id}).mappings().first())

    # additional candidate can be added as per the table definition pa_track nominated_post_proposal_candidate
    def add_candidate(self, proposal_id, item, created_by, created_by_name):
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        status = (proposal.get("current_status_code") or "").upper()
        if status in _CANDIDATE_ADD_BLOCKED_STATUSES:
            raise ValueError("Candidates cannot be modified after GO has been issued or the proposal is closed")
        snap = self.get_candidate_snapshot_from_nomination_candidate(item.nominationPostCandidateId) if item.nominationPostCandidateId else self.get_candidate_snapshot_from_cadre(item.tdpCadreId)
        if not snap:
            raise ValueError("Candidate/profile not found in existing database")
        cadre_id = snap.get("tdpCadreId")
        npc_id = item.nominationPostCandidateId or snap.get("nominationPostCandidateId")
        legacy_links = self.legacy_repo.resolve_candidate_links(
            npc_id,
            nominated_post_member_id=proposal.get("nominated_post_member_id"),
            enrollment_id=proposal.get("enrollment_id"),
        )
        nominated_post_id = item.nominatedPostId or legacy_links.get("nominatedPostId") or 0
        nominated_post_application_id = (
            item.nominatedPostApplicationId
            or legacy_links.get("nominatedPostApplicationId")
            or 0
        )
        existing = self._find_proposal_candidate(proposal_id, cadre_id)
        if existing and existing.get("is_deleted") == "N":
            return "skipped"
        params = {
            "proposal_id": proposal_id, "tdp_cadre_id": cadre_id,
            "membership_id": snap.get("membershipId"), "nomination_post_candidate_id": npc_id,
            "nominated_post_id": nominated_post_id, "nominated_post_application_id": nominated_post_application_id,
            "candidate_name": snap.get("candidateName"), "mobile_no": snap.get("mobileNo"),
            "gender": snap.get("gender"), "age": snap.get("age"), "caste_state_id": snap.get("casteStateId"),
            "source_type": item.sourceType, "remarks": item.remarks, "created_by": created_by, "created_by_name": created_by_name,
        }
        if existing and existing.get("is_deleted") == "Y":
            self.pa_track_db.execute(text("""
                UPDATE nominated_post_proposal_candidate SET
                    is_deleted='N',
                    membership_id=:membership_id,
                    nomination_post_candidate_id=:nomination_post_candidate_id,
                    nominated_post_id=:nominated_post_id,
                    nominated_post_application_id=:nominated_post_application_id,
                    candidate_name=:candidate_name,
                    mobile_no=:mobile_no,
                    gender=:gender,
                    age=:age,
                    caste_state_id=:caste_state_id,
                    source_type=:source_type,
                    remarks=:remarks,
                    candidate_status='ADDED',
                    is_selected='N',
                    updated_by=:created_by,
                    updated_by_name=:created_by_name,
                    updated_time=NOW()
                WHERE proposal_candidate_id=:proposal_candidate_id
            """), {**params, "proposal_candidate_id": existing["proposal_candidate_id"]})
            return "reactivated"
        self.pa_track_db.execute(text("""
            INSERT INTO nominated_post_proposal_candidate (
                proposal_id,tdp_cadre_id,membership_id,nomination_post_candidate_id,nominated_post_id,nominated_post_application_id,
                candidate_name,mobile_no,gender,age,caste_state_id,source_type,remarks,created_by,created_by_name
            ) VALUES (
                :proposal_id,:tdp_cadre_id,:membership_id,:nomination_post_candidate_id,:nominated_post_id,:nominated_post_application_id,
                :candidate_name,:mobile_no,:gender,:age,:caste_state_id,:source_type,:remarks,:created_by,:created_by_name
            )
        """), params)
        return "added"

    def add_manual_candidate(self, proposal_id, req):
        """Insert a brand-new candidate (not in tdp_cadre) directly on the proposal.

        Row carries tdp_cadre_id = NULL and source_type = 'MANUAL'; the unique key
        uk_prop_cadre_active (proposal_id, tdp_cadre_id, is_deleted) does not collide
        because MySQL treats NULL tdp_cadre_id as distinct.
        """
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        status = (proposal.get("current_status_code") or "").upper()
        if status in _CANDIDATE_ADD_BLOCKED_STATUSES:
            raise ValueError("Candidates cannot be modified after GO has been issued or the proposal is closed")
        if int(req.partyId) not in _MANUAL_CANDIDATE_PARTY_IDS:
            raise ValueError("Invalid party selected")
        self.pa_track_db.execute(text("""
            INSERT INTO nominated_post_proposal_candidate (
                proposal_id,tdp_cadre_id,candidate_name,mobile_no,gender,age,date_of_birth,
                caste_state_id,caste_name,caste_category_name,
                occupation_id,occupation_name,education_id,education_name,
                parliament_id,parliament_name,assembly_id,assembly_name,mandal_id,mandal_name,
                party_id,party_short_name,
                source_type,remarks,created_by,created_by_name
            ) VALUES (
                :proposal_id,NULL,:candidate_name,:mobile_no,:gender,:age,:date_of_birth,
                :caste_state_id,:caste_name,:caste_category_name,
                :occupation_id,:occupation_name,:education_id,:education_name,
                :parliament_id,:parliament_name,:assembly_id,:assembly_name,:mandal_id,:mandal_name,
                :party_id,:party_short_name,
                'MANUAL',:remarks,:created_by,:created_by_name
            )
        """), {
            "proposal_id": proposal_id,
            "candidate_name": req.candidateName, "mobile_no": req.mobileNo,
            "gender": req.gender, "age": req.age, "date_of_birth": req.dob,
            "caste_state_id": req.casteStateId, "caste_name": req.casteName,
            "caste_category_name": req.casteCategoryName,
            "occupation_id": req.occupationId, "occupation_name": req.occupationName,
            "education_id": req.educationId, "education_name": req.educationName,
            "parliament_id": req.parliamentId, "parliament_name": req.parliamentName,
            "assembly_id": req.assemblyId, "assembly_name": req.assemblyName,
            "mandal_id": req.mandalId, "mandal_name": req.mandalName,
            "party_id": req.partyId, "party_short_name": req.partyShortName,
            "remarks": req.remarks, "created_by": req.createdBy, "created_by_name": req.createdByName,
        })
        return self.pa_track_db.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().first()["id"]

    @staticmethod
    def _manual_candidate_profile(cand: dict) -> dict:
        """Build a profile (matching CadreProfileRepository's shape) from the snapshot
        columns of a MANUAL candidate, so the UI renders it like any other candidate."""
        dob = cand.get("date_of_birth")
        return {
            "tdpCadreId": None,
            "membershipId": None,
            "mid": "",
            "candidateName": cand.get("candidate_name") or "",
            "mobileNo": cand.get("mobile_no"),
            "gender": cand.get("gender"),
            "age": cand.get("age"),
            "dob": str(dob) if dob is not None else None,
            "occupation": cand.get("occupation_name"),
            "education": cand.get("education_name"),
            "casteStateId": cand.get("caste_state_id"),
            "casteName": cand.get("caste_name"),
            "castCategory": cand.get("caste_category_name"),
            "mandal": cand.get("mandal_name"),
            "assembly": cand.get("assembly_name"),
            "parliament": cand.get("parliament_name"),
            "partyId": cand.get("party_id"),
            "partyShortName": cand.get("party_short_name"),
            "photoUrl": None,
            "constituencyPercent": None,
            "renewalTimes": None,
            "isManual": True,
        }

    def remove_candidate(self, proposal_id, proposal_candidate_id, action_by, action_by_name):
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        status = (proposal.get("current_status_code") or "").upper()
        if status in _CANDIDATE_ADD_BLOCKED_STATUSES:
            raise ValueError("Candidates cannot be modified after GO has been issued or the proposal is closed")
        row = self._row(self.pa_track_db.execute(text("""
            SELECT proposal_candidate_id, candidate_name
            FROM nominated_post_proposal_candidate
            WHERE proposal_candidate_id=:proposal_candidate_id
              AND proposal_id=:proposal_id AND is_deleted='N'
        """), {
            "proposal_candidate_id": proposal_candidate_id,
            "proposal_id": proposal_id,
        }).mappings().first())
        if not row:
            raise ValueError("Candidate not found on this proposal")
        self.pa_track_db.execute(text("""
            UPDATE nominated_post_proposal_candidate
            SET is_deleted='Y',
                updated_by=:action_by,
                updated_by_name=:action_by_name,
                updated_time=NOW()
            WHERE proposal_candidate_id=:proposal_candidate_id
              AND proposal_id=:proposal_id AND is_deleted='N'
        """), {
            "proposal_candidate_id": proposal_candidate_id,
            "proposal_id": proposal_id,
            "action_by": action_by,
            "action_by_name": action_by_name,
        })
        return row.get("candidate_name") or ""

    def remove_all_candidates(self, proposal_id, action_by, action_by_name):
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        status = (proposal.get("current_status_code") or "").upper()
        if status in _CANDIDATE_ADD_BLOCKED_STATUSES:
            raise ValueError("Candidates cannot be modified after GO has been issued or the proposal is closed")
        result = self.pa_track_db.execute(text("""
            UPDATE nominated_post_proposal_candidate
            SET is_deleted='Y',
                updated_by=:action_by,
                updated_by_name=:action_by_name,
                updated_time=NOW()
            WHERE proposal_id=:proposal_id AND is_deleted='N'
        """), {
            "proposal_id": proposal_id,
            "action_by": action_by,
            "action_by_name": action_by_name,
        })
        return int(result.rowcount or 0)

    def get_workflow_stage_code(self, proposal_id: int) -> str | None:
        meta = self._workflow_meta_for_proposals([int(proposal_id)])
        row = meta.get(int(proposal_id), {})
        code = row.get("workflow_stage_code")
        if not code:
            return None
        return str(code).upper()

    def _soft_delete_workflow_instance(self, proposal_id: int) -> None:
        self.pa_track_db.execute(text("""
            UPDATE workflow_instance
            SET is_deleted = 'Y',
                updated_time = NOW()
            WHERE reference_table_name = 'nominated_post_proposal'
              AND reference_id = :proposal_id
              AND is_deleted = 'N'
        """), {"proposal_id": proposal_id})

    def _hard_delete_proposal(self, proposal_id: int) -> dict:
        self._soft_delete_workflow_instance(proposal_id)

        self.pa_track_db.execute(text("""
            DELETE FROM proposal_feedback
            WHERE proposal_id = :proposal_id
        """), {"proposal_id": proposal_id})

        self.pa_track_db.execute(text("""
            DELETE FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
        """), {"proposal_id": proposal_id})

        self.pa_track_db.execute(text("""
            DELETE FROM nominated_post_proposal_audit
            WHERE proposal_id = :proposal_id
        """), {"proposal_id": proposal_id})

        self.pa_track_db.execute(text("""
            DELETE FROM proposal_workflow_history
            WHERE proposal_id = :proposal_id
        """), {"proposal_id": proposal_id})

        self.pa_track_db.execute(text("""
            DELETE FROM nominated_post_proposal
            WHERE proposal_id = :proposal_id
        """), {"proposal_id": proposal_id})

        return {"proposalId": proposal_id, "deleted": True, "mode": "hard"}

    def soft_delete_proposal(
        self,
        proposal_id: int,
        action_by: int = 0,
        action_by_name: str = "",
    ) -> dict:
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")

        self.pa_track_db.execute(text("""
            UPDATE nominated_post_proposal
            SET is_deleted = 'Y',
                updated_by = :action_by,
                updated_by_name = :action_by_name,
                updated_time = NOW()
            WHERE proposal_id = :proposal_id AND is_deleted = 'N'
        """), {
            "proposal_id": proposal_id,
            "action_by": action_by,
            "action_by_name": action_by_name or "",
        })

        self._soft_delete_workflow_instance(proposal_id)

        from_status = proposal.get("current_status_code")
        self.insert_audit(
            proposal_id,
            "SOFT_DELETE_PROPOSAL",
            from_status,
            from_status,
            action_by,
            action_by_name,
            "Proposal removed from active list (soft delete)",
            None,
        )

        return {"proposalId": proposal_id, "deleted": True, "mode": "soft"}

    def delete_proposal(
        self,
        proposal_id: int,
        action_by: int = 0,
        action_by_name: str = "",
    ) -> dict:
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")

        stage = self.get_workflow_stage_code(proposal_id)
        if stage is None or stage in _HARD_DELETE_WORKFLOW_STAGES:
            result = self._hard_delete_proposal(proposal_id)
        else:
            result = self.soft_delete_proposal(proposal_id, action_by, action_by_name)

        self.pa_track_db.commit()
        return result

    def update_proposed_count(self, proposal_id):
        self.pa_track_db.execute(text("""
            UPDATE nominated_post_proposal p SET proposed_count = (
              SELECT COUNT(*) FROM nominated_post_proposal_candidate c WHERE c.proposal_id=p.proposal_id AND c.is_deleted='N'
            ) WHERE p.proposal_id=:proposal_id
        """), {"proposal_id": proposal_id})

    def get_existing_attached_candidates(self, nominated_post_member_id):
        return self.legacy_repo.get_existing_attached_candidates(nominated_post_member_id)

    def _attach_legacy_context_to_candidates(self, candidates: list[dict]) -> list[dict]:
        npc_ids = [
            int(c["nomination_post_candidate_id"])
            for c in candidates
            if c.get("nomination_post_candidate_id")
        ]
        if not npc_ids:
            return candidates
        legacy_by_npc = self.legacy_repo.build_legacy_context_batch(npc_ids)
        for cand in candidates:
            npc_id = cand.get("nomination_post_candidate_id")
            if npc_id:
                cand["legacy"] = legacy_by_npc.get(int(npc_id), {})
        return candidates

    def get_proposal_header(self, proposal_id):
        return self._row(self.pa_track_db.execute(text("""
            SELECT * FROM nominated_post_proposal WHERE proposal_id=:proposal_id AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().first())

    def get_proposal_detail(self, proposal_id):
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            return None
        candidates = self._attach_legacy_context_to_candidates(
            self._rows(self.pa_track_db.execute(text("""
                SELECT * FROM nominated_post_proposal_candidate
                WHERE proposal_id=:proposal_id AND is_deleted='N'
            """), {"proposal_id": proposal_id}).mappings().all())
        )
        # Attach a base cadre profile so already-added candidates show full details
        # (caste, assembly, parliament, mandal, occupation, photo) regardless of
        # whether report_ratings is configured. When the service has report_ratings,
        # it overwrites this with the richer performance profile downstream.
        profiles = self.profile_repo.get_profiles_by_cadre_ids(
            c.get("tdp_cadre_id") for c in candidates
        )
        for cand in candidates:
            cadre_id = cand.get("tdp_cadre_id")
            if cadre_id and int(cadre_id) in profiles:
                cand["profile"] = profiles[int(cadre_id)]
            elif not cadre_id and (cand.get("source_type") or "").upper() == "MANUAL":
                # Manual candidates have no tdp_cadre row — build a profile from the snapshot.
                cand["profile"] = self._manual_candidate_profile(cand)
        proposal["candidates"] = candidates
        proposal["audit"] = self._rows(self.pa_track_db.execute(text("""
            SELECT * FROM nominated_post_proposal_audit WHERE proposal_id=:proposal_id ORDER BY audit_id
        """), {"proposal_id": proposal_id}).mappings().all())
        proposal["existingAttachedCandidates"] = self.get_existing_attached_candidates(proposal["nominated_post_member_id"])
        proposal["feedbacks"] = self._rows(self.pa_track_db.execute(text("""
            SELECT proposal_candidate_id AS proposalCandidateId,
                   feedback_code        AS feedbackCode,
                   feedback_text        AS feedbackText,
                   no_feedback_required AS noFeedbackRequired
            FROM proposal_feedback
            WHERE proposal_id=:proposal_id AND is_deleted='N'
            ORDER BY proposal_feedback_id
        """), {"proposal_id": proposal_id}).mappings().all())
        proposal["reviews"] = self._rows(self.pa_track_db.execute(text("""
            SELECT proposal_candidate_id AS proposalCandidateId,
                   reviewer_role_code    AS reviewerRoleCode,
                   rank_value            AS rankValue,
                   review_comments       AS reviewComments
            FROM proposal_review
            WHERE proposal_id=:proposal_id AND is_deleted='N'
            ORDER BY proposal_review_id
        """), {"proposal_id": proposal_id}).mappings().all())
        proposal["goDetails"] = self._rows(self.pa_track_db.execute(text("""
            SELECT
                proposal_go_id AS proposalGoId,
                proposal_candidate_id AS proposalCandidateId,
                go_number AS goNumber,
                go_issue_date AS goIssueDate,
                go_remarks AS goRemarks,
                legacy_sync_status AS legacySyncStatus
            FROM proposal_go_details
            WHERE proposal_id=:proposal_id AND is_deleted='N'
            ORDER BY proposal_go_id
        """), {"proposal_id": proposal_id}).mappings().all())
        return proposal

    def list_candidates_for_proposals(self, proposal_ids: list[int]):
        if not proposal_ids:
            return []
        placeholders = ", ".join([f":id_{idx}" for idx, _ in enumerate(proposal_ids)])
        params = {f"id_{idx}": int(pid) for idx, pid in enumerate(proposal_ids)}
        return self._rows(self.pa_track_db.execute(text(f"""
            SELECT
                proposal_candidate_id, proposal_id, tdp_cadre_id, membership_id,
                candidate_name, mobile_no, gender, age, candidate_status, is_selected,
                nomination_post_candidate_id, source_type, remarks
            FROM nominated_post_proposal_candidate
            WHERE is_deleted='N'
              AND proposal_id IN ({placeholders})
            ORDER BY proposal_candidate_id DESC
        """), params).mappings().all())

    def list_active_candidate_mids(self, proposal_id: int) -> list[str]:
        rows = self.pa_track_db.execute(text("""
            SELECT membership_id
            FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
              AND is_deleted = 'N'
              AND membership_id IS NOT NULL
              AND membership_id != ''
        """), {"proposal_id": proposal_id}).mappings().all()
        mids = []
        seen = set()
        for row in rows:
            mid = str(row["membership_id"]).strip().replace("#", "")
            if mid and mid not in seen:
                seen.add(mid)
                mids.append(mid)
        return mids

    def resolve_location_display(self, board_level_id, location_value, existing_name=None):
        if existing_name:
            return existing_name
        if location_value in (None, "", 0):
            return existing_name

        lookups = []
        level_id = int(board_level_id or 0)
        if level_id == 2:
            lookups = [("state", "state_id", "state_name")]
        elif level_id == 3:
            lookups = [("district", "district_id", "district_name")]
        else:
            lookups = [
                ("constituency", "constituency_id", "constituency_name"),
                ("district", "district_id", "district_name"),
                ("state", "state_id", "state_name"),
            ]

        for table, id_col, name_col in lookups:
            try:
                row = self.dakavara_db.execute(text(f"""
                    SELECT {name_col} AS locationName
                    FROM {table}
                    WHERE {id_col}=:location_value
                    LIMIT 1
                """), {"location_value": location_value}).mappings().first()
                if row and row.get("locationName"):
                    return row["locationName"]
            except Exception:
                continue
        return existing_name

    def list_proposals(
        self,
        enrollment_id: int = 2,
        limit: int = 200,
        offset: int = 0,
        status_code: str | None = None,
    ):
        where = "WHERE p.is_deleted='N' AND p.enrollment_id=:enrollment_id"
        params: dict = {"enrollment_id": enrollment_id, "limit": limit, "offset": offset}
        if status_code:
            where += " AND p.current_status_code=:status_code"
            params["status_code"] = status_code

        proposals = self._rows(self.pa_track_db.execute(text(f"""
            SELECT p.*
            FROM nominated_post_proposal p
            {where}
            ORDER BY COALESCE(p.updated_time, p.created_time) DESC
            LIMIT :limit OFFSET :offset
        """), params).mappings().all())

        if not proposals:
            return []

        proposal_ids = [int(p["proposal_id"]) for p in proposals]
        candidates = self.list_candidates_for_proposals(proposal_ids)
        by_proposal: dict[int, list] = {}
        for cand in candidates:
            by_proposal.setdefault(int(cand["proposal_id"]), []).append(cand)

        workflow_by_proposal = self._workflow_meta_for_proposals(proposal_ids)

        for proposal in proposals:
            pid = int(proposal["proposal_id"])
            cands = by_proposal.get(pid, [])
            proposal["candidates"] = cands
            proposal["candidate_count"] = len(cands)
            proposal["shortlisted_count"] = sum(
                1 for c in cands if (c.get("candidate_status") or "").upper() == "SHORTLISTED"
            )
            proposal["selected_count"] = sum(
                1 for c in cands
                if (c.get("candidate_status") or "").upper() == "SELECTED" or c.get("is_selected") == "Y"
            )
            wf = workflow_by_proposal.get(pid, {})
            proposal["workflow_stage_code"] = wf.get("workflow_stage_code")
            proposal["workflow_display_order"] = wf.get("workflow_display_order")
            proposal["location_display"] = (
                (proposal.get("location_name") or "").strip()
                or proposal.get("board_level_name")
                or "—"
            )

        return proposals

    def _workflow_meta_for_proposals(self, proposal_ids: list[int]):
        if not proposal_ids:
            return {}
        placeholders = ", ".join([f":id_{idx}" for idx, _ in enumerate(proposal_ids)])
        params = {f"id_{idx}": int(pid) for idx, pid in enumerate(proposal_ids)}
        rows = self._rows(self.pa_track_db.execute(text(f"""
            SELECT
                wi.reference_id AS proposal_id,
                wsm.stage_code AS workflow_stage_code,
                wsm.display_order AS workflow_display_order
            FROM workflow_instance wi
            LEFT JOIN workflow_stage_master wsm
              ON wsm.workflow_stage_id = wi.current_stage_id
            WHERE wi.is_deleted = 'N'
              AND wi.reference_table_name = 'nominated_post_proposal'
              AND wi.reference_id IN ({placeholders})
        """), params).mappings().all())
        return {int(r["proposal_id"]): r for r in rows}

    def update_proposal_status(
        self,
        proposal_id: int,
        status_code: str,
        remarks: str | None = None,
        action_by: int = 0,
        action_by_name: str = "",
    ):
        proposal = self.get_proposal_header(proposal_id)
        if not proposal:
            return None
        from_status = proposal.get("current_status_code")
        status_id = self.get_status_id(status_code)
        self.pa_track_db.execute(
            text("""
                UPDATE nominated_post_proposal
                SET current_status_id = :status_id,
                    current_status_code = :status_code,
                    updated_time = NOW(),
                    updated_by = :action_by,
                    updated_by_name = :action_by_name
                WHERE proposal_id = :proposal_id AND is_deleted = 'N'
            """),
            {
                "proposal_id": proposal_id,
                "status_id": status_id,
                "status_code": status_code,
                "action_by": action_by,
                "action_by_name": action_by_name or "",
            },
        )
        if remarks:
            self.pa_track_db.execute(
                text("""
                    UPDATE nominated_post_proposal
                    SET remarks = :remarks
                    WHERE proposal_id = :proposal_id AND is_deleted = 'N'
                """),
                {"proposal_id": proposal_id, "remarks": remarks},
            )
        self.insert_audit(
            proposal_id,
            "UPDATE_STATUS",
            from_status,
            status_code,
            action_by,
            action_by_name,
            f"Status changed to {status_code}",
            remarks,
        )
        self.pa_track_db.commit()
        return self.get_proposal_detail(proposal_id)

    def revert_to_previous_stage(self, proposal_id: int, action_by: int = 0, action_by_name: str = ""):
        """Move the workflow instance one stage back (used by the detail "Back" button).
        Data-driven: picks the stage with the greatest display_order below the current
        one, so it always lands on the immediately preceding step. Persists to
        workflow_instance (which drives the UI step on refresh) and best-effort syncs
        the proposal status. No-op when already at the first stage.

        Returns a light payload (not the heavy get_proposal_detail): the caller ignores
        the body and re-fetches, and the enrichment query added several seconds against
        the remote DB."""
        if not self.get_proposal_header(proposal_id):
            return None

        inst = self.pa_track_db.execute(text("""
            SELECT wi.workflow_instance_id, wi.workflow_id, wi.current_stage_code,
                   cur.display_order AS cur_order
            FROM workflow_instance wi
            JOIN workflow_stage_master cur ON cur.workflow_stage_id = wi.current_stage_id
            WHERE wi.reference_table_name='nominated_post_proposal'
              AND wi.reference_id=:proposal_id AND wi.is_deleted='N'
            LIMIT 1
        """), {"proposal_id": proposal_id}).mappings().first()
        # No workflow instance yet (still on the merged Add-Profiles tab) or already at
        # the first stage — nothing to move on the server; let the UI navigate back.
        if not inst:
            return {"proposal_id": proposal_id, "reverted": False}

        prev = self.pa_track_db.execute(text("""
            SELECT workflow_stage_id, stage_code, mapped_proposal_status_code
            FROM workflow_stage_master
            WHERE workflow_id=:workflow_id AND is_active='Y' AND is_deleted='N'
              AND display_order < :cur_order
            ORDER BY display_order DESC
            LIMIT 1
        """), {"workflow_id": inst["workflow_id"], "cur_order": inst["cur_order"]}).mappings().first()
        if not prev:
            return {"proposal_id": proposal_id, "reverted": False}

        self.pa_track_db.execute(text("""
            UPDATE workflow_instance
            SET current_stage_id=:stage_id, current_stage_code=:stage_code,
                completed_time=NULL, updated_by=:action_by
            WHERE workflow_instance_id=:instance_id
        """), {
            "stage_id": prev["workflow_stage_id"], "stage_code": prev["stage_code"],
            "action_by": action_by, "instance_id": inst["workflow_instance_id"],
        })

        target_status = prev["mapped_proposal_status_code"] or prev["stage_code"]
        try:
            status_id = self.get_status_id(target_status)
        except ValueError:
            status_id = None
        if status_id:
            self.pa_track_db.execute(text("""
                UPDATE nominated_post_proposal
                SET current_status_id=:status_id, current_status_code=:status_code,
                    updated_time=NOW(), updated_by=:action_by, updated_by_name=:action_by_name
                WHERE proposal_id=:proposal_id AND is_deleted='N'
            """), {
                "status_id": status_id, "status_code": target_status,
                "action_by": action_by, "action_by_name": action_by_name or "",
                "proposal_id": proposal_id,
            })

        self.insert_audit(
            proposal_id, "REVERT_STAGE", inst["current_stage_code"], prev["stage_code"],
            action_by, action_by_name, f"Moved back to {prev['stage_code']}", None,
        )
        self.pa_track_db.commit()
        return {"proposal_id": proposal_id, "reverted": True, "stage_code": prev["stage_code"]}

    def insert_audit(self, proposal_id, action_code, from_status, to_status, action_by, action_by_name, comments, remarks):
        self.pa_track_db.execute(text("""
            INSERT INTO nominated_post_proposal_audit (proposal_id,action_code,from_status_code,to_status_code,comments,remarks,action_by,action_by_name)
            VALUES (:proposal_id,:action_code,:from_status,:to_status,:comments,:remarks,:action_by,:action_by_name)
        """), locals())

    def insert_event(self, aggregate_type, aggregate_id, event_type, payload):
        self.pa_track_db.execute(text("""
            INSERT INTO event_outbox (aggregate_type,aggregate_id,event_type,payload_json)
            VALUES (:aggregate_type,:aggregate_id,:event_type,CAST(:payload_json AS JSON))
        """), {"aggregate_type":aggregate_type,"aggregate_id":aggregate_id,"event_type":event_type,"payload_json":json.dumps(payload)})
