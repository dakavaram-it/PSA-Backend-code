import json
import logging
from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session

from app.repositories.cadre_profile_repository import CadreProfileRepository

logger = logging.getLogger(__name__)

# Stages where a committee proposal is still a draft — deleting it wipes the rows
# outright. Past this point the proposal is soft-deleted so its trail survives.
_HARD_DELETE_WORKFLOW_STAGES = frozenset({"ADD_PROFILES"})


class CommitteeRepository:
    def __init__(self, pa_track_db: Session, legacy_db: Session):
        self.db = pa_track_db
        self.legacy_db = legacy_db
        self.profile_repo = CadreProfileRepository(legacy_db)

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    @staticmethod
    def _rows(rows):
        return [dict(r) for r in rows]

    # ---------- Master / Lookup ----------

    def get_committee_type(self, committee_type_code: str):
        return self._row(self.db.execute(text("""
            SELECT * FROM pa_track.committee_type_master
            WHERE committee_type_code=:code
              AND is_active='Y'
              AND is_deleted='N'
        """), {"code": committee_type_code}).mappings().first())

    def get_status(self, status_code: str):
        return self._row(self.db.execute(text("""
            SELECT * FROM pa_track.committee_status_master
            WHERE status_code=:status_code
              AND is_active='Y'
              AND is_deleted='N'
        """), {"status_code": status_code}).mappings().first())

    def get_basic_committees(self, committee_type_id: int):
        """
        Step 2 — list of basic committees for the selected committee type.
        tdp_committee_type_id: 1 = Main, 2 = Affiliated.
        """
        rows = self.legacy_db.execute(text("""
            SELECT tdp_basic_committee_id AS id,
                   name AS name,
                   tdp_committee_type_id AS committeeTypeId
            FROM dakavara_pa.tdp_basic_committee
            WHERE tdp_committee_type_id = :committee_type_id
            ORDER BY name
        """), {"committee_type_id": committee_type_id}).mappings().all()
        return self._rows(rows)

    def get_committees(self, level_id: int, enrollment_id: int, level_value: int | None,
                       basic_committee_id: int | None = None, committee_type_id: int | None = None):
        """
        Step 3 — committees that exist for a given level/location/enrollment.
        Optionally narrowed to a single basic committee or a committee type
        (1 = Main, 2 = Affiliated). Returns tdp_committee_id used to fetch roles.
        Only committees that actually exist for the selection are returned, so the
        UI can render them as selectable cards (like Nominated Post departments).

        Each card carries seat accounting aggregated across all of the committee's
        roles (mirrors the Nominated Post department display):
          totalSeats    = SUM of every role's max_members
          filledSeats   = SUM of active members per role (capped at the role's seats)
          unfilledSeats = SUM of max(role total - role filled, 0)

        Seat accounting is fetched in a SECOND query scoped to only the committees
        returned by the first one. tdp_committee_role (~6.8M rows) and
        tdp_committee_member (~2.9M rows) are huge; aggregating them inside a derived
        table joined to the committee list forces MySQL to materialise the full-table
        aggregation before applying the level/enrollment filter (~45s). Scoping the
        aggregation to the handful of in-view committee ids — both join columns are
        indexed — keeps it sub-second.
        """
        rows = self.legacy_db.execute(text("""
            SELECT TBC.tdp_basic_committee_id AS tdpBasicCommitteeId,
                   TBC.name AS basicCommitteeName,
                   TBC.tdp_committee_type_id AS committeeTypeId,
                   TC.tdp_committee_id AS tdpCommitteeId,
                   COALESCE(TC.name, TBC.name) AS committeeName,
                   TC.tdp_committee_level_id AS levelId,
                   TC.tdp_committee_level_value AS levelValue,
                   TC.tdp_committee_enrollment_id AS enrollmentId
            FROM dakavara_pa.tdp_committee TC
            JOIN dakavara_pa.tdp_basic_committee TBC
              ON TC.tdp_basic_committee_id = TBC.tdp_basic_committee_id
            WHERE TC.tdp_committee_level_id = :level_id
              AND TC.tdp_committee_enrollment_id = :enrollment_id
              AND (:level_value IS NULL OR TC.tdp_committee_level_value = :level_value)
              AND (:basic_committee_id IS NULL OR TC.tdp_basic_committee_id = :basic_committee_id)
              AND (:committee_type_id IS NULL OR TBC.tdp_committee_type_id = :committee_type_id)
            ORDER BY TBC.tdp_committee_type_id, COALESCE(TC.name, TBC.name)
        """), {
            "level_id": level_id,
            "enrollment_id": enrollment_id,
            "level_value": level_value,
            "basic_committee_id": basic_committee_id,
            "committee_type_id": committee_type_id,
        }).mappings().all()

        result = [dict(r) for r in rows]
        committee_ids = [r["tdpCommitteeId"] for r in result if r.get("tdpCommitteeId") is not None]
        seats_by_committee = self._get_committee_seat_counts(committee_ids)

        for row in result:
            seat = seats_by_committee.get(row.get("tdpCommitteeId"), {})
            row["totalSeats"] = int(seat.get("totalSeats") or 0)
            row["filledSeats"] = int(seat.get("filledSeats") or 0)
            row["unfilledSeats"] = int(seat.get("unfilledSeats") or 0)
        return result

    def _get_committee_seat_counts(self, committee_ids):
        """
        Seat totals (totalSeats / filledSeats / unfilledSeats) per committee, scoped
        to the given committee ids. Per-role counts are computed first (a role's seats
        must not be double-counted across its members), then summed per committee with
        each role's filled count capped at its seats. Returns {committeeId: {...}}.
        """
        if not committee_ids:
            return {}
        seat_rows = self.legacy_db.execute(
            text("""
                SELECT RS.tdp_committee_id                    AS tdpCommitteeId,
                       SUM(RS.total)                          AS totalSeats,
                       SUM(LEAST(RS.filled, RS.total))        AS filledSeats,
                       SUM(GREATEST(RS.total - RS.filled, 0)) AS unfilledSeats
                FROM (
                    SELECT TCR.tdp_committee_id,
                           TCR.tdp_committee_role_id,
                           TCR.max_members                  AS total,
                           COUNT(DISTINCT TCM.tdp_cadre_id) AS filled
                    FROM dakavara_pa.tdp_committee_role TCR
                    LEFT OUTER JOIN dakavara_pa.tdp_committee_member TCM
                      ON TCR.tdp_committee_role_id = TCM.tdp_committee_role_id
                     AND TCM.is_active = 'Y'
                    WHERE TCR.tdp_committee_id IN :committee_ids
                    GROUP BY TCR.tdp_committee_id, TCR.tdp_committee_role_id, TCR.max_members
                ) RS
                GROUP BY RS.tdp_committee_id
            """).bindparams(bindparam("committee_ids", expanding=True)),
            {"committee_ids": committee_ids},
        ).mappings().all()
        return {r["tdpCommitteeId"]: dict(r) for r in seat_rows}

    def get_committee_roles(self, tdp_committee_id: int):
        """
        Step 4 & 5 — roles for the selected committee with seat accounting.
        assigned = currently active members; max_members = total seats.
        Mirrors the Nominated Post position display:
          totalSeats / filledSeats / unfilledSeats, selectable only when unfilled > 0.
        """
        rows = self.legacy_db.execute(text("""
            SELECT TCR.tdp_committee_role_id AS tdpCommitteeRoleId,
                   TR.tdp_roles_id AS tdpRolesId,
                   TR.role AS roleName,
                   TCR.max_members AS maxMembers,
                   COUNT(DISTINCT TCM.tdp_cadre_id) AS assigned
            FROM dakavara_pa.tdp_committee TC
            JOIN dakavara_pa.tdp_committee_role TCR
              ON TC.tdp_committee_id = TCR.tdp_committee_id
            JOIN dakavara_pa.tdp_roles TR
              ON TCR.tdp_roles_id = TR.tdp_roles_id
            LEFT OUTER JOIN dakavara_pa.tdp_committee_member TCM
              ON TCR.tdp_committee_role_id = TCM.tdp_committee_role_id
             AND TCM.is_active = 'Y'
            WHERE TC.tdp_committee_id = :tdp_committee_id
            GROUP BY TCR.tdp_committee_role_id, TR.tdp_roles_id, TR.role, TCR.max_members, TR.`order`
            ORDER BY TR.`order`
        """), {"tdp_committee_id": tdp_committee_id}).mappings().all()

        result = []
        for r in rows:
            total = int(r["maxMembers"] or 0)
            filled = int(r["assigned"] or 0)
            unfilled = max(total - filled, 0)
            result.append({
                "tdpCommitteeRoleId": int(r["tdpCommitteeRoleId"]),
                "tdpRolesId": int(r["tdpRolesId"]) if r["tdpRolesId"] is not None else None,
                "roleName": r["roleName"],
                "totalSeats": total,
                "filledSeats": filled,
                "unfilledSeats": unfilled,
                "canPropose": unfilled > 0,
            })
        return result

    def check_kss_membership(self, membership_id: str):
        """
        KSS gate for committee candidates: a cadre is "part of KSS" only if they
        hold an active committee membership at the KSS level (tdp_committee_level_id = 18,
        enrollment 4). Returns the matching memberships so the UI can show context.
        """
        mid = (membership_id or "").strip().lstrip("#")
        if not mid:
            raise ValueError("membership id (mid) is required")

        rows = self.legacy_db.execute(text("""
            SELECT TCL.tdp_committee_level AS committeeLevel,
                   TCT.committee_type      AS committeeType,
                   TBC.name                AS committeeName,
                   TR.role                 AS roleName,
                   CR.membership_id        AS membershipId,
                   CR.first_name           AS firstName,
                   CR.mobile_no            AS mobileNo
            FROM tdp_committee TC
            JOIN tdp_committee_level TCL
              ON TC.tdp_committee_level_id = TCL.tdp_committee_level_id
             AND TC.tdp_committee_enrollment_id = 4
            JOIN tdp_basic_committee TBC ON TC.tdp_basic_committee_id = TBC.tdp_basic_committee_id
            JOIN tdp_committee_type TCT  ON TBC.tdp_committee_type_id = TCT.tdp_committee_type_id
            JOIN tdp_committee_role TCR  ON TC.tdp_committee_id = TCR.tdp_committee_id
            JOIN tdp_roles TR           ON TCR.tdp_roles_id = TR.tdp_roles_id
            JOIN tdp_committee_member TCM
              ON TCR.tdp_committee_role_id = TCM.tdp_committee_role_id
             AND TCM.is_active = 'Y'
            JOIN tdp_cadre CR ON TCM.tdp_cadre_id = CR.tdp_cadre_id
            WHERE TCL.tdp_committee_level_id = 18
              AND CR.membership_id = :membership_id
        """), {"membership_id": mid}).mappings().all()

        return {
            "membershipId": mid,
            "isKss": len(rows) > 0,
            "memberships": self._rows(rows),
        }

    def get_workflow(self):
        return self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_definition
            WHERE post_type_id=1
              AND workflow_code='COMMITTEE_WORKFLOW'
              AND is_active='Y'
              AND is_deleted='N'
            ORDER BY version_no DESC
            LIMIT 1
        """)).mappings().first())

    # ---------- Proposal ----------

    def create_proposal(self, req):
        committee_type = self.get_committee_type(req.committeeTypeCode)
        if not committee_type:
            raise ValueError(f"Invalid committeeTypeCode: {req.committeeTypeCode}")

        status = self.get_status("ADD_PROFILES")
        if not status:
            raise ValueError("Committee status ADD_PROFILES is not configured")

        # If an active proposal already exists for this exact committee/level/type,
        # return it instead of creating a duplicate — the UI opens the existing one
        # so the operator continues adding candidates there.
        existing = self._find_active_duplicate_proposal(req)
        if existing:
            return self.get_proposal(existing["committee_proposal_id"])

        self.db.execute(text("""
            INSERT INTO pa_track.committee_proposal (
                post_type_id, committee_type_id, committee_type_code,
                legacy_tdp_committee_id, legacy_tdp_basic_committee_id,
                legacy_tdp_committee_level_id, legacy_tdp_committee_level_value,
                legacy_tdp_committee_enrollment_id, legacy_committee_confirm_rule_id,
                committee_name, committee_level_name, location_name,
                current_status_id, current_status_code,
                remarks, created_by, created_by_name
            )
            VALUES (
                1, :committee_type_id, :committee_type_code,
                :legacy_tdp_committee_id, :legacy_tdp_basic_committee_id,
                :legacy_tdp_committee_level_id, :legacy_tdp_committee_level_value,
                :legacy_tdp_committee_enrollment_id, :legacy_committee_confirm_rule_id,
                :committee_name, :committee_level_name, :location_name,
                :status_id, 'ADD_PROFILES',
                :remarks, :created_by, :created_by_name
            )
        """), {
            "committee_type_id": committee_type["committee_type_id"],
            "committee_type_code": committee_type["committee_type_code"],
            "legacy_tdp_committee_id": req.legacyTdpCommitteeId,
            "legacy_tdp_basic_committee_id": req.legacyTdpBasicCommitteeId,
            "legacy_tdp_committee_level_id": req.legacyTdpCommitteeLevelId,
            "legacy_tdp_committee_level_value": req.legacyTdpCommitteeLevelValue,
            "legacy_tdp_committee_enrollment_id": req.legacyTdpCommitteeEnrollmentId,
            "legacy_committee_confirm_rule_id": req.legacyCommitteeConfirmRuleId,
            "committee_name": req.committeeName,
            "committee_level_name": req.committeeLevelName,
            "location_name": req.locationName,
            "status_id": status["status_id"],
            "remarks": req.remarks,
            "created_by": req.createdBy,
            "created_by_name": req.createdByName,
        })

        proposal_id = self.db.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().first()["id"]

        for pos in req.positions:
            self.db.execute(text("""
                INSERT INTO pa_track.committee_proposal_position (
                    committee_proposal_id, legacy_tdp_committee_role_id, legacy_tdp_roles_id,
                    role_name, role_type, seats_required, max_members_snapshot,
                    min_propose_members_snapshot, max_propose_members_snapshot,
                    already_proposed_snapshot, already_selected_snapshot,
                    inserted_by
                )
                VALUES (
                    :proposal_id, :legacy_role_id, :legacy_tdp_roles_id,
                    :role_name, :role_type, :seats_required, :max_members_snapshot,
                    :min_prop, :max_prop, :already_prop, :already_sel, :inserted_by
                )
            """), {
                "proposal_id": proposal_id,
                "legacy_role_id": pos.legacyTdpCommitteeRoleId,
                "legacy_tdp_roles_id": pos.legacyTdpRolesId,
                "role_name": pos.roleName,
                "role_type": pos.roleType,
                "seats_required": pos.seatsRequired,
                "max_members_snapshot": pos.maxMembersSnapshot,
                "min_prop": pos.minProposeMembersSnapshot,
                "max_prop": pos.maxProposeMembersSnapshot,
                "already_prop": pos.alreadyProposedSnapshot,
                "already_sel": pos.alreadySelectedSnapshot,
                "inserted_by": req.createdBy,
            })

        self.insert_audit(proposal_id, "CREATE_COMMITTEE_PROPOSAL", None, "ADD_PROFILES", req.remarks, None, req.createdBy, req.createdByName)
        self.insert_event(proposal_id, "committee.proposal.created", {"committeeProposalId": proposal_id})
        self.db.commit()
        return self.get_proposal(proposal_id)

    # ---------- Delete ----------

    def get_workflow_stage_code(self, proposal_id: int) -> str | None:
        row = self.db.execute(text("""
            SELECT current_stage_code
            FROM pa_track.workflow_instance
            WHERE post_type_id = 1
              AND reference_table_name = 'committee_proposal'
              AND reference_id = :proposal_id
              AND is_deleted = 'N'
        """), {"proposal_id": proposal_id}).mappings().first()
        code = row["current_stage_code"] if row else None
        return str(code).upper() if code else None

    def _soft_delete_workflow_instance(self, proposal_id: int) -> None:
        self.db.execute(text("""
            UPDATE pa_track.workflow_instance
            SET is_deleted = 'Y'
            WHERE reference_table_name = 'committee_proposal'
              AND reference_id = :proposal_id
              AND is_deleted = 'N'
        """), {"proposal_id": proposal_id})

    def _hard_delete_proposal(self, proposal_id: int) -> dict:
        self._soft_delete_workflow_instance(proposal_id)

        for table in (
            "committee_feedback",
            "committee_review",
            "committee_finalization",
            "committee_legacy_sync_log",
            "committee_proposal_member",
            "committee_proposal_position",
            "committee_proposal_audit",
            "committee_workflow_history",
            "committee_proposal",
        ):
            self.db.execute(
                text(f"DELETE FROM pa_track.{table} WHERE committee_proposal_id = :proposal_id"),
                {"proposal_id": proposal_id},
            )

        return {"committeeProposalId": proposal_id, "deleted": True, "mode": "hard"}

    def soft_delete_proposal(self, proposal_id: int, action_by: int = 0, action_by_name: str = "") -> dict:
        proposal = self._require_proposal(proposal_id)

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal
            SET is_deleted = 'Y',
                updated_by = :action_by,
                updated_time = NOW()
            WHERE committee_proposal_id = :proposal_id AND is_deleted = 'N'
        """), {"proposal_id": proposal_id, "action_by": action_by})

        self._soft_delete_workflow_instance(proposal_id)

        from_status = proposal.get("current_status_code")
        self.insert_audit(
            proposal_id,
            "SOFT_DELETE_PROPOSAL",
            from_status,
            from_status,
            "Committee proposal removed from active list (soft delete)",
            None,
            action_by,
            action_by_name,
        )

        return {"committeeProposalId": proposal_id, "deleted": True, "mode": "soft"}

    def delete_proposal(self, proposal_id: int, action_by: int = 0, action_by_name: str = "") -> dict:
        self._require_proposal(proposal_id)

        stage = self.get_workflow_stage_code(proposal_id)
        if stage is None or stage in _HARD_DELETE_WORKFLOW_STAGES:
            result = self._hard_delete_proposal(proposal_id)
        else:
            result = self.soft_delete_proposal(proposal_id, action_by, action_by_name)

        self.db.commit()
        return result

    def get_proposal(self, proposal_id: int):
        proposal = self._row(self.db.execute(text("""
            SELECT * FROM pa_track.committee_proposal
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().first())
        if not proposal:
            return None

        positions = self._rows(self.db.execute(text("""
            SELECT * FROM pa_track.committee_proposal_position
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
            ORDER BY committee_proposal_position_id
        """), {"proposal_id": proposal_id}).mappings().all())

        members = self._rows(self.db.execute(text("""
            SELECT * FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
            ORDER BY committee_proposal_member_id
        """), {"proposal_id": proposal_id}).mappings().all())

        # Attach full cadre profile (caste, assembly, parliament, mandal, occupation,
        # photo) so already-added members show complete details on the detail screen.
        profiles = self.profile_repo.get_profiles_by_cadre_ids(
            m.get("tdp_cadre_id") for m in members
        )
        for member in members:
            cadre_id = member.get("tdp_cadre_id")
            if cadre_id and int(cadre_id) in profiles:
                member["profile"] = profiles[int(cadre_id)]

        # Saved feedback / reviews per member, shaped like the Nominated Post detail
        # (proposalCandidateId == committee_proposal_member_id) so the shared detail
        # screen can pre-populate prior input when reopening the Feedback / Review step.
        proposal["feedbacks"] = self._rows(self.db.execute(text("""
            SELECT committee_proposal_member_id AS proposalCandidateId,
                   feedback_code                AS feedbackCode,
                   feedback_text                AS feedbackText,
                   no_feedback_required         AS noFeedbackRequired
            FROM pa_track.committee_feedback
            WHERE committee_proposal_id=:proposal_id AND is_deleted='N'
            ORDER BY committee_proposal_member_id
        """), {"proposal_id": proposal_id}).mappings().all())
        proposal["reviews"] = self._rows(self.db.execute(text("""
            SELECT committee_proposal_member_id AS proposalCandidateId,
                   reviewer_role_code           AS reviewerRoleCode,
                   rank_value                   AS rankValue,
                   review_comments              AS reviewComments
            FROM pa_track.committee_review
            WHERE committee_proposal_id=:proposal_id AND is_deleted='N'
            ORDER BY committee_proposal_member_id
        """), {"proposal_id": proposal_id}).mappings().all())

        proposal["positions"] = positions
        proposal["members"] = members
        return proposal

    def list_proposals(self, limit: int = 200, status_code: str | None = None):
        """List committee proposals (newest first) with their positions and members,
        mirroring the Nominated Post proposal list so the UI can rebuild state after a refresh.
        Each row carries `workflow_stage_code` (from workflow_instance), `positions[]`,
        `members[]`, and a `candidate_count`."""
        rows = self._rows(self.db.execute(text("""
            SELECT cp.*, wi.current_stage_code AS workflow_stage_code
            FROM pa_track.committee_proposal cp
            LEFT JOIN pa_track.workflow_instance wi
              ON wi.post_type_id = 1
             AND wi.reference_table_name = 'committee_proposal'
             AND wi.reference_id = cp.committee_proposal_id
             AND wi.is_deleted = 'N'
            WHERE cp.is_deleted = 'N'
              AND (:status_code IS NULL OR cp.current_status_code = :status_code)
            ORDER BY cp.committee_proposal_id DESC
            LIMIT :limit
        """), {"status_code": status_code, "limit": limit}).mappings().all())
        if not rows:
            return []

        ids = [r["committee_proposal_id"] for r in rows]

        positions = self._rows(self.db.execute(
            text("""
                SELECT * FROM pa_track.committee_proposal_position
                WHERE committee_proposal_id IN :ids AND is_deleted='N'
                ORDER BY committee_proposal_position_id
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).mappings().all())

        members = self._rows(self.db.execute(
            text("""
                SELECT * FROM pa_track.committee_proposal_member
                WHERE committee_proposal_id IN :ids AND is_deleted='N'
                ORDER BY committee_proposal_member_id
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).mappings().all())

        pos_by, mem_by = {}, {}
        for p in positions:
            pos_by.setdefault(p["committee_proposal_id"], []).append(p)
        for m in members:
            mem_by.setdefault(m["committee_proposal_id"], []).append(m)

        for r in rows:
            cid = r["committee_proposal_id"]
            r["positions"] = pos_by.get(cid, [])
            r["members"] = mem_by.get(cid, [])
            r["candidate_count"] = len(mem_by.get(cid, []))
        return rows

    # ---------- Members ----------

    def add_members(self, proposal_id: int, req):
        proposal = self._require_proposal(proposal_id)
        # Members may be added at any active stage — a proposal already ahead in the
        # workflow (FEEDBACK / REVIEW_APPROVAL / FINALISING) can still take new
        # candidates when the operator steps back to the Add Candidates screen. Only
        # terminal stages are blocked, mirroring remove_members and the Nominated Post
        # add flow (which likewise blocks only closed proposals).
        if proposal["current_status_code"] in ("COMPLETED", "REJECTED"):
            raise ValueError(f"Members cannot be added when committee is {proposal['current_status_code']}")

        # NOTE: the legacy "already finalized in a confirmed committee" block was
        # intentionally removed. KSS membership is itself an active membership in a
        # confirmed committee (level 18), so that check rejected every valid KSS
        # candidate. The KSS gate (checked before search) is now the controlling rule,
        # matching the Nominated Post add flow which has no such restriction.
        #
        # The per-candidate validation lookups are hoisted into one query each
        # (proposal positions, legacy cadre existence/active state, and the existing
        # position+candidate pairs), validated in memory, then inserted in a single
        # batched round-trip — avoids 5 queries per candidate.
        position_ids = self._proposal_position_ids(proposal_id)
        cadre_status = self._cadre_status_map(item.tdpCadreId for item in req.members)
        existing_pairs = self._existing_member_pairs(proposal_id)
        # Soft-deleted (is_deleted='Y') rows keyed by (position, cadre). Re-adding a
        # previously-removed cadre must REACTIVATE its tombstone row, not INSERT a fresh
        # one: uk_committee_candidate_position is (position, cadre, is_deleted), so a
        # duplicate 'N' row would later collide with the surviving 'Y' row the moment
        # either is soft-deleted again (the 409 seen on DELETE). Mirrors Nominated Post.
        soft_deleted = self._soft_deleted_member_ids_by_pair(proposal_id)

        seen = set()
        params = []
        reactivate = []
        for item in req.members:
            key = (item.committeeProposalPositionId, item.tdpCadreId)
            if key in seen:
                raise ValueError(f"Duplicate candidate in request. position={item.committeeProposalPositionId}, tdpCadreId={item.tdpCadreId}")
            seen.add(key)

            if item.committeeProposalPositionId not in position_ids:
                raise ValueError(f"Invalid committeeProposalPositionId {item.committeeProposalPositionId} for proposal {proposal_id}")
            if item.tdpCadreId not in cadre_status:
                raise ValueError(f"Candidate does not exist in legacy tdp_cadre: {item.tdpCadreId}")
            if cadre_status[item.tdpCadreId] not in (None, "N"):
                raise ValueError(f"Candidate is inactive/deleted in legacy tdp_cadre: {item.tdpCadreId}")
            if key in existing_pairs:
                raise ValueError(f"Candidate already added to this committee position: {item.tdpCadreId}")

            row = {
                "proposal_id": proposal_id,
                "position_id": item.committeeProposalPositionId,
                "tdp_cadre_id": item.tdpCadreId,
                "membership_id": item.membershipId,
                "candidate_name": item.candidateName,
                "mobile_no": item.mobileNo,
                "gender": item.gender,
                "age": item.age,
                "caste_state_id": item.casteStateId,
                "source_type": item.sourceType,
                "remarks": item.remarks,
                "inserted_by": req.createdBy,
                "inserted_by_name": req.createdByName,
            }
            if key in soft_deleted:
                reactivate.append({**row, "member_id": soft_deleted[key]})
            else:
                params.append(row)

        # Reactivate tombstoned rows back to a fresh 'ADDED' state (matches a new insert).
        for row in reactivate:
            self.db.execute(text("""
                UPDATE pa_track.committee_proposal_member SET
                    is_deleted='N', member_status='ADDED', is_selected='N',
                    membership_id=:membership_id, candidate_name=:candidate_name,
                    mobile_no=:mobile_no, gender=:gender, age=:age,
                    caste_state_id=:caste_state_id, source_type=:source_type,
                    remarks=:remarks, updated_by=:inserted_by, updated_time=NOW()
                WHERE committee_proposal_member_id=:member_id
            """), row)

        if params:
            self.db.execute(text("""
                INSERT INTO pa_track.committee_proposal_member (
                    committee_proposal_id, committee_proposal_position_id,
                    tdp_cadre_id, membership_id, candidate_name, mobile_no,
                    gender, age, caste_state_id, source_type, remarks,
                    inserted_by, inserted_by_name
                )
                VALUES (
                    :proposal_id, :position_id, :tdp_cadre_id, :membership_id,
                    :candidate_name, :mobile_no, :gender, :age, :caste_state_id,
                    :source_type, :remarks, :inserted_by, :inserted_by_name
                )
            """), params)

        self.insert_audit(proposal_id, "ADD_COMMITTEE_MEMBERS", proposal["current_status_code"], proposal["current_status_code"], None, None, req.createdBy, req.createdByName)
        self.insert_event(proposal_id, "committee.members.added", {"committeeProposalId": proposal_id, "count": len(req.members)})
        self.db.commit()
        return self.get_proposal(proposal_id)

    def remove_member(self, proposal_id: int, member_id: int, user_id: int = 0, user_name: str | None = None):
        proposal = self._require_proposal(proposal_id)
        if proposal["current_status_code"] in ("COMPLETED", "REJECTED"):
            raise ValueError(f"Cannot remove members when committee is {proposal['current_status_code']}")
        self._validate_member_belongs_to_proposal(proposal_id, member_id)

        # Soft-deleting sets is_deleted='Y', which collides on uk_committee_candidate_position
        # (position, cadre, is_deleted) if an older tombstone for the same position+cadre
        # already exists (from a pre-fix add/remove cycle). That redundant, already-deleted
        # row carries no live data, so hard-delete it first to free the unique slot.
        self.db.execute(text("""
            DELETE twin FROM pa_track.committee_proposal_member twin
            JOIN pa_track.committee_proposal_member cur
              ON twin.committee_proposal_position_id = cur.committee_proposal_position_id
             AND twin.tdp_cadre_id = cur.tdp_cadre_id
            WHERE cur.committee_proposal_id=:proposal_id
              AND cur.committee_proposal_member_id=:member_id
              AND twin.committee_proposal_member_id <> cur.committee_proposal_member_id
              AND twin.is_deleted='Y'
        """), {"proposal_id": proposal_id, "member_id": member_id})

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal_member
            SET is_deleted='Y', updated_by=:user_id, updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
              AND committee_proposal_member_id=:member_id
        """), {"proposal_id": proposal_id, "member_id": member_id, "user_id": user_id})

        self.insert_audit(proposal_id, "REMOVE_COMMITTEE_MEMBER",
                          proposal["current_status_code"], proposal["current_status_code"],
                          None, None, user_id, user_name)
        self.insert_event(proposal_id, "committee.member.removed",
                          {"committeeProposalId": proposal_id, "committeeProposalMemberId": member_id})
        self.db.commit()
        return self.get_proposal(proposal_id)

    def remove_all_members(self, proposal_id: int, user_id: int = 0, user_name: str | None = None):
        proposal = self._require_proposal(proposal_id)
        if proposal["current_status_code"] in ("COMPLETED", "REJECTED"):
            raise ValueError(f"Cannot remove members when committee is {proposal['current_status_code']}")

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal_member
            SET is_deleted='Y', updated_by=:user_id, updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id, "user_id": user_id})

        self.insert_audit(proposal_id, "REMOVE_ALL_COMMITTEE_MEMBERS",
                          proposal["current_status_code"], proposal["current_status_code"],
                          None, None, user_id, user_name)
        self.insert_event(proposal_id, "committee.members.removed_all",
                          {"committeeProposalId": proposal_id})
        self.db.commit()
        return self.get_proposal(proposal_id)

    def update_status(self, proposal_id: int, status_code: str, user_id: int = 0,
                      user_name: str | None = None, remarks: str | None = None):
        """Direct status override (used by the Fresh Cycle reset to ADD_PROFILES).
        Keeps committee_proposal and its workflow_instance stage in sync."""
        proposal = self._require_proposal(proposal_id)
        status = self.get_status(status_code)
        if not status:
            raise ValueError(f"Invalid committee status: {status_code}")

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal
            SET current_status_id=:status_id,
                current_status_code=:status_code,
                updated_by=:user_id,
                updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
        """), {
            "status_id": status["status_id"],
            "status_code": status_code,
            "user_id": user_id,
            "proposal_id": proposal_id,
        })

        workflow = self.get_workflow()
        stage = self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_stage_master
            WHERE workflow_id=:workflow_id
              AND stage_code=:stage_code
              AND is_active='Y'
              AND is_deleted='N'
        """), {"workflow_id": workflow["workflow_id"], "stage_code": status_code}).mappings().first())
        if stage:
            self.db.execute(text("""
                UPDATE pa_track.workflow_instance
                SET current_stage_id=:stage_id,
                    current_stage_code=:stage_code,
                    completed_time=NULL,
                    updated_by=:user_id
                WHERE post_type_id=1
                  AND reference_table_name='committee_proposal'
                  AND reference_id=:proposal_id
                  AND is_deleted='N'
            """), {
                "stage_id": stage["workflow_stage_id"],
                "stage_code": status_code,
                "user_id": user_id,
                "proposal_id": proposal_id,
            })

        self.insert_audit(proposal_id, "UPDATE_STATUS",
                          proposal["current_status_code"], status_code,
                          remarks, remarks, user_id, user_name)
        self.insert_event(proposal_id, "committee.status.updated",
                          {"committeeProposalId": proposal_id, "toStatus": status_code})
        self.db.commit()
        return self.get_proposal(proposal_id)

    def revert_to_previous_stage(self, proposal_id: int, user_id: int = 0, user_name: str | None = None):
        """Move the workflow instance one stage back (detail "Back" button).
        Data-driven: picks the stage with the greatest display_order below the current
        one, so it always lands on the immediately preceding step. Persists to
        workflow_instance (which drives the UI step on refresh) and best-effort syncs
        the proposal status. No-op when already at the first stage.

        Returns a light payload (not the heavy get_proposal): the caller ignores the
        body and re-fetches, and get_proposal fans out to positions + members, adding
        several seconds against the remote DB."""
        exists = self._row(self.db.execute(text("""
            SELECT committee_proposal_id FROM pa_track.committee_proposal
            WHERE committee_proposal_id=:proposal_id AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().first())
        if not exists:
            raise ValueError(f"Committee proposal not found: {proposal_id}")

        inst = self._row(self.db.execute(text("""
            SELECT wi.workflow_instance_id, wi.workflow_id, wi.current_stage_code,
                   cur.display_order AS cur_order
            FROM pa_track.workflow_instance wi
            JOIN pa_track.workflow_stage_master cur ON cur.workflow_stage_id = wi.current_stage_id
            WHERE wi.reference_table_name='committee_proposal'
              AND wi.reference_id=:proposal_id AND wi.is_deleted='N'
            LIMIT 1
        """), {"proposal_id": proposal_id}).mappings().first())
        if not inst:
            return {"committee_proposal_id": proposal_id, "reverted": False}

        prev = self._row(self.db.execute(text("""
            SELECT workflow_stage_id, stage_code, mapped_proposal_status_code
            FROM pa_track.workflow_stage_master
            WHERE workflow_id=:workflow_id AND is_active='Y' AND is_deleted='N'
              AND display_order < :cur_order
            ORDER BY display_order DESC
            LIMIT 1
        """), {"workflow_id": inst["workflow_id"], "cur_order": inst["cur_order"]}).mappings().first())
        if not prev:
            return {"committee_proposal_id": proposal_id, "reverted": False}

        self.db.execute(text("""
            UPDATE pa_track.workflow_instance
            SET current_stage_id=:stage_id, current_stage_code=:stage_code,
                completed_time=NULL, updated_by=:user_id
            WHERE workflow_instance_id=:instance_id
        """), {
            "stage_id": prev["workflow_stage_id"], "stage_code": prev["stage_code"],
            "user_id": user_id, "instance_id": inst["workflow_instance_id"],
        })

        target_status = prev["mapped_proposal_status_code"] or prev["stage_code"]
        status = self.get_status(target_status)
        if status:
            self.db.execute(text("""
                UPDATE pa_track.committee_proposal
                SET current_status_id=:status_id, current_status_code=:status_code,
                    updated_by=:user_id, updated_time=NOW()
                WHERE committee_proposal_id=:proposal_id
            """), {
                "status_id": status["status_id"], "status_code": target_status,
                "user_id": user_id, "proposal_id": proposal_id,
            })

        self.insert_audit(proposal_id, "REVERT_STAGE",
                          inst["current_stage_code"], prev["stage_code"],
                          f"Moved back to {prev['stage_code']}", None, user_id, user_name)
        self.db.commit()
        return {"committee_proposal_id": proposal_id, "reverted": True, "stage_code": prev["stage_code"]}

    # ---------- Workflow ----------

    def get_or_create_instance(self, proposal: dict, workflow: dict, user_id: int, user_name: str | None):
        inst = self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_instance
            WHERE post_type_id=1
              AND reference_table_name='committee_proposal'
              AND reference_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal["committee_proposal_id"]}).mappings().first())
        if inst:
            return inst

        stage = self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_stage_master
            WHERE workflow_id=:workflow_id
              AND stage_code=:stage_code
              AND is_active='Y'
              AND is_deleted='N'
        """), {
            "workflow_id": workflow["workflow_id"],
            "stage_code": proposal["current_status_code"]
        }).mappings().first())

        self.db.execute(text("""
            INSERT INTO pa_track.workflow_instance (
                workflow_id, post_type_id, reference_table_name, reference_id,
                current_stage_id, current_stage_code, started_by, started_by_name, inserted_by
            )
            VALUES (
                :workflow_id, 1, 'committee_proposal', :proposal_id,
                :stage_id, :stage_code, :started_by, :started_by_name, :inserted_by
            )
        """), {
            "workflow_id": workflow["workflow_id"],
            "proposal_id": proposal["committee_proposal_id"],
            "stage_id": stage["workflow_stage_id"],
            "stage_code": stage["stage_code"],
            "started_by": user_id,
            "started_by_name": user_name,
            "inserted_by": user_id,
        })

        iid = self.db.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().first()["id"]
        return self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_instance WHERE workflow_instance_id=:id
        """), {"id": iid}).mappings().first())

    def get_transition(self, workflow_id: int, from_stage_id: int, action_code: str, role_code: str):
        role = self._row(self.db.execute(text("""
            SELECT * FROM pa_track.workflow_role_master
            WHERE role_code=:role_code AND is_active='Y' AND is_deleted='N'
        """), {"role_code": role_code}).mappings().first())
        if not role:
            raise ValueError(f"Invalid roleCode: {role_code}")

        trans = self._row(self.db.execute(text("""
            SELECT t.*, a.action_code, fs.stage_code fromStageCode, ts.stage_code toStageCode,
                   ts.mapped_proposal_status_code toProposalStatusCode
            FROM pa_track.workflow_transition_master t
            JOIN pa_track.workflow_action_master a ON t.action_id=a.workflow_action_id
            JOIN pa_track.workflow_stage_master fs ON t.from_stage_id=fs.workflow_stage_id
            JOIN pa_track.workflow_stage_master ts ON t.to_stage_id=ts.workflow_stage_id
            WHERE t.workflow_id=:workflow_id
              AND t.from_stage_id=:from_stage_id
              AND a.action_code=:action_code
              AND t.is_active='Y'
              AND t.is_deleted='N'
        """), {
            "workflow_id": workflow_id,
            "from_stage_id": from_stage_id,
            "action_code": action_code
        }).mappings().first())

        if not trans:
            raise ValueError(f"Action {action_code} is not allowed from current stage")

        if role["is_admin_role"] == "Y":
            return trans

        allowed = self._row(self.db.execute(text("""
            SELECT 1 FROM pa_track.workflow_stage_role_mapping
            WHERE workflow_id=:workflow_id
              AND stage_id=:stage_id
              AND role_id=:role_id
              AND (action_id IS NULL OR action_id=:action_id)
              AND can_act='Y'
              AND is_active='Y'
              AND is_deleted='N'
            LIMIT 1
        """), {
            "workflow_id": workflow_id,
            "stage_id": from_stage_id,
            "role_id": role["workflow_role_id"],
            "action_id": trans["action_id"],
        }).mappings().first())

        if not allowed:
            raise ValueError(f"Role {role_code} cannot perform {action_code}")
        return trans

    def allowed_actions(self, proposal_id: int, role_code: str):
        proposal = self._require_proposal(proposal_id)
        workflow = self.get_workflow()
        inst = self.get_or_create_instance(proposal, workflow, proposal["created_by"], proposal.get("created_by_name"))

        rows = self._rows(self.db.execute(text("""
            SELECT a.action_code actionCode, a.action_name actionName, a.button_label buttonLabel,
                   ts.stage_code toStageCode, ts.stage_name toStageName
            FROM pa_track.workflow_transition_master t
            JOIN pa_track.workflow_action_master a ON t.action_id=a.workflow_action_id
            JOIN pa_track.workflow_stage_master ts ON t.to_stage_id=ts.workflow_stage_id
            WHERE t.workflow_id=:workflow_id
              AND t.from_stage_id=:stage_id
              AND t.is_active='Y'
              AND t.is_deleted='N'
            ORDER BY t.display_order
        """), {
            "workflow_id": workflow["workflow_id"],
            "stage_id": inst["current_stage_id"]
        }).mappings().all())

        return {
            "committeeProposalId": proposal_id,
            "currentStageCode": inst["current_stage_code"],
            "roleCode": role_code,
            "allowedActions": rows
        }

    def execute_action(self, proposal_id: int, req, enable_committee_legacy_sync: bool):
        proposal = self._require_proposal(proposal_id)
        workflow = self.get_workflow()
        inst = self.get_or_create_instance(proposal, workflow, req.actionBy, req.actionByName)

        # Re-finalisation: a committee that already reached COMPLETED can be finalised
        # again after the operator goes back, edits feedback/reviews, and changes the
        # member selection. COMPLETED is terminal (no outgoing transition), so we bypass
        # get_transition and re-run the finalisation in place — the stage stays COMPLETED.
        if req.actionCode == "FINALISE_COMMITTEE" and proposal["current_status_code"] == "COMPLETED":
            return self._re_finalise(proposal_id, inst, workflow, req, enable_committee_legacy_sync)

        trans = self.get_transition(workflow["workflow_id"], inst["current_stage_id"], req.actionCode, req.roleCode)

        logger.info("committee workflow action proposal_id=%s action=%s from=%s", proposal_id, req.actionCode, inst["current_stage_code"])

        if req.feedbacks:
            self.save_feedbacks(workflow["workflow_id"], proposal_id, req.feedbacks, req.actionBy, req.actionByName)

        if req.reviews:
            self.save_reviews(proposal_id, req.reviews, req.actionBy, req.actionByName)

        if trans["requires_feedback_validation"] == "Y":
            self.validate_feedback(workflow["workflow_id"], proposal_id)

        if trans["requires_review_validation"] == "Y":
            self.validate_review(proposal_id)

        # Strict gate — no active member may cross into FINALISING or COMPLETED without
        # BOTH feedback and a review. The flag-driven checks above are stage-specific
        # (feedback validated on MOVE_TO_REVIEW, review on MOVE_TO_FINALISING), so a
        # member added after those stages — proposal already at REVIEW_APPROVAL /
        # FINALISING — would otherwise reach the committee un-vetted. Runs after the
        # inline feedback/review saves above, so records submitted with this action count.
        if trans["toStageCode"] == "FINALISING" or req.actionCode == "FINALISE_COMMITTEE":
            self.validate_feedback(workflow["workflow_id"], proposal_id)
            self.validate_review(proposal_id)

        if trans["discard_existing_candidates"] == "Y":
            self.discard_members(proposal_id)

        if req.actionCode == "FINALISE_COMMITTEE":
            if not req.selectedCommitteeProposalMemberIds:
                raise ValueError("selectedCommitteeProposalMemberIds is required")
            self.finalise_members(proposal_id, req.selectedCommitteeProposalMemberIds, enable_committee_legacy_sync, req.actionBy, req.actionByName)

        self.insert_history(inst, proposal_id, trans, req.roleCode, req)
        self.insert_audit(proposal_id, req.actionCode, inst["current_stage_code"], trans["toStageCode"], req.comments, req.remarks, req.actionBy, req.actionByName)
        self.update_stage(proposal_id, inst["workflow_instance_id"], trans, req.actionBy)
        self.insert_event(proposal_id, f"committee.workflow.{req.actionCode.lower()}", {
            "committeeProposalId": proposal_id,
            "fromStage": inst["current_stage_code"],
            "toStage": trans["toStageCode"],
            "actionCode": req.actionCode,
        })
        self.db.commit()

        return {
            "committeeProposalId": proposal_id,
            "fromStage": inst["current_stage_code"],
            "toStage": trans["toStageCode"],
            "actionCode": req.actionCode
        }

    def _re_finalise(self, proposal_id: int, inst, workflow, req, enable_committee_legacy_sync: bool):
        """Re-run finalisation on an already-COMPLETED committee with the operator's
        updated feedback/reviews/selection. Stage stays COMPLETED — no transition."""
        logger.info("committee re-finalise proposal_id=%s (already COMPLETED)", proposal_id)

        if not req.selectedCommitteeProposalMemberIds:
            raise ValueError("selectedCommitteeProposalMemberIds is required")

        if req.feedbacks:
            # save_feedbacks soft-deletes prior rows for the rewritten members before
            # inserting, so re-finalisation no longer appends duplicates. (Reviews
            # already upsert on their unique key, so they need no equivalent cleanup.)
            self.save_feedbacks(workflow["workflow_id"], proposal_id, req.feedbacks, req.actionBy, req.actionByName)
        if req.reviews:
            self.save_reviews(proposal_id, req.reviews, req.actionBy, req.actionByName)

        self.finalise_members(proposal_id, req.selectedCommitteeProposalMemberIds,
                              enable_committee_legacy_sync, req.actionBy, req.actionByName)

        self.insert_audit(proposal_id, "RE_FINALISE_COMMITTEE", "COMPLETED", "COMPLETED",
                          req.comments, req.remarks, req.actionBy, req.actionByName)
        self.insert_event(proposal_id, "committee.workflow.re_finalise_committee", {
            "committeeProposalId": proposal_id,
            "fromStage": "COMPLETED",
            "toStage": "COMPLETED",
            "actionCode": "FINALISE_COMMITTEE",
        })
        self.db.commit()

        return {
            "committeeProposalId": proposal_id,
            "fromStage": "COMPLETED",
            "toStage": "COMPLETED",
            "actionCode": "FINALISE_COMMITTEE"
        }

    def save_feedbacks_only(self, proposal_id: int, feedbacks: list, user_id: int, user_name: str | None):
        """Persist feedback without a stage change. Lets the operator save/edit feedback
        even after the committee has moved past the FEEDBACK stage — previously the only
        write path was inline on the MOVE_TO_REVIEW transition, so later edits were lost."""
        self._require_proposal(proposal_id)
        workflow = self.get_workflow()
        self.save_feedbacks(workflow["workflow_id"], proposal_id, feedbacks, user_id, user_name)
        self.db.commit()
        return {"committeeProposalId": proposal_id, "savedFeedbacks": len(feedbacks or [])}

    def save_reviews_only(self, proposal_id: int, reviews: list, user_id: int, user_name: str | None):
        """Persist reviews without a stage change — so reviews typed in the Review step
        are saved as entered, not only inline on the MOVE_TO_FINALISING transition."""
        self._require_proposal(proposal_id)
        self.save_reviews(proposal_id, reviews, user_id, user_name)
        self.db.commit()
        return {"committeeProposalId": proposal_id, "savedReviews": len(reviews or [])}

    # ---------- Feedback / Review / Finalisation ----------

    def save_feedbacks(self, workflow_id: int, proposal_id: int, feedbacks: list, user_id: int, user_name: str | None):
        if not feedbacks:
            return
        # committee_feedback has no unique key, so re-saving would append duplicate rows
        # (re-entering the Feedback step, or saving without a stage change). Soft-delete
        # the prior feedback for the members being (re)written, then insert the fresh set
        # — same approach as the Nominated Post save_feedbacks.
        written_member_ids = {fb.committeeProposalMemberId for fb in feedbacks}
        if written_member_ids:
            self.db.execute(text("""
                UPDATE pa_track.committee_feedback
                SET is_deleted='Y'
                WHERE committee_proposal_id=:proposal_id
                  AND committee_proposal_member_id IN :member_ids
                  AND is_deleted='N'
            """).bindparams(bindparam("member_ids", expanding=True)),
            {"proposal_id": proposal_id, "member_ids": list(written_member_ids)})
        # Hoist the per-feedback lookups out of the loop: member membership and the
        # feedback-type table are fetched once, then validation runs in memory. The
        # inserts are batched into a single executemany round-trip.
        member_ids = self._proposal_member_ids(proposal_id)
        feedback_types = self._feedback_types_by_code(workflow_id)

        params = []
        for fb in feedbacks:
            if fb.committeeProposalMemberId not in member_ids:
                raise ValueError(f"Invalid committeeProposalMemberId {fb.committeeProposalMemberId} for proposal {proposal_id}")
            ft = feedback_types.get(fb.feedbackCode)
            if not ft:
                raise ValueError(f"Invalid feedbackCode: {fb.feedbackCode}")
            params.append({
                "proposal_id": proposal_id,
                "member_id": fb.committeeProposalMemberId,
                "feedback_type_id": ft["feedback_type_id"],
                "feedback_code": fb.feedbackCode,
                "feedback_text": fb.feedbackText,
                "no_feedback_required": fb.noFeedbackRequired,
                "submitted_by": user_id,
                "submitted_by_name": user_name
            })

        self.db.execute(text("""
            INSERT INTO pa_track.committee_feedback (
                committee_proposal_id, committee_proposal_member_id,
                feedback_type_id, feedback_code, feedback_text,
                no_feedback_required, submitted_by, submitted_by_name
            )
            VALUES (
                :proposal_id, :member_id,
                :feedback_type_id, :feedback_code, :feedback_text,
                :no_feedback_required, :submitted_by, :submitted_by_name
            )
        """), params)

    def validate_feedback(self, workflow_id: int, proposal_id: int):
        mandatory = self._rows(self.db.execute(text("""
            SELECT feedback_code
            FROM pa_track.workflow_feedback_type_master
            WHERE workflow_id=:workflow_id
              AND is_mandatory='Y'
              AND is_active='Y'
              AND is_deleted='N'
        """), {"workflow_id": workflow_id}).mappings().all())
        mandatory_codes = {x["feedback_code"] for x in mandatory}

        members = self._rows(self.db.execute(text("""
            SELECT committee_proposal_member_id
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND member_status NOT IN ('DISCARDED','REJECTED','NOT_SELECTED')
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all())

        # Fetch all feedback for the proposal in one query and group by member,
        # instead of one SELECT per member (N+1).
        feedback_by_member = {}
        for fb in self.db.execute(text("""
            SELECT committee_proposal_member_id, feedback_code, no_feedback_required
            FROM pa_track.committee_feedback
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all():
            feedback_by_member.setdefault(fb["committee_proposal_member_id"], []).append(fb)

        errors = []
        for m in members:
            member_id = m["committee_proposal_member_id"]
            rows = feedback_by_member.get(member_id, [])

            if not rows:
                errors.append(f"Member {member_id} has no feedback or no-feedback-required record")
                continue

            if any(x["no_feedback_required"] == "Y" for x in rows):
                continue

            codes = {x["feedback_code"] for x in rows}
            missing = mandatory_codes - codes
            if missing:
                errors.append(f"Member {member_id} missing feedbacks: {', '.join(sorted(missing))}")

        if errors:
            raise ValueError("; ".join(errors))

    def save_reviews(self, proposal_id: int, reviews: list, user_id: int, user_name: str | None):
        if not reviews:
            return
        # Member membership and the role table are fetched once, validated in memory,
        # and the upserts are batched into a single executemany round-trip.
        member_ids = self._proposal_member_ids(proposal_id)
        roles_by_code = self._roles_by_code()

        params = []
        for rv in reviews:
            if rv.committeeProposalMemberId not in member_ids:
                raise ValueError(f"Invalid committeeProposalMemberId {rv.committeeProposalMemberId} for proposal {proposal_id}")
            role = roles_by_code.get(rv.reviewerRoleCode)
            if not role:
                raise ValueError(f"Invalid reviewerRoleCode: {rv.reviewerRoleCode}")
            params.append({
                "proposal_id": proposal_id,
                "member_id": rv.committeeProposalMemberId,
                "role_id": role["workflow_role_id"],
                "role_code": role["role_code"],
                "role_label": role["role_label"],
                "rank_value": rv.rankValue,
                "review_status": rv.reviewStatus,
                "review_comments": rv.reviewComments,
                "reviewed_by": user_id,
                "reviewed_by_name": user_name
            })

        self.db.execute(text("""
            INSERT INTO pa_track.committee_review (
                committee_proposal_id, committee_proposal_member_id,
                reviewer_role_id, reviewer_role_code, reviewer_label,
                rank_value, review_status, review_comments,
                reviewed_by, reviewed_by_name
            )
            VALUES (
                :proposal_id, :member_id,
                :role_id, :role_code, :role_label,
                :rank_value, :review_status, :review_comments,
                :reviewed_by, :reviewed_by_name
            )
            ON DUPLICATE KEY UPDATE
                rank_value=VALUES(rank_value),
                review_status=VALUES(review_status),
                review_comments=VALUES(review_comments),
                reviewed_by=VALUES(reviewed_by),
                reviewed_by_name=VALUES(reviewed_by_name),
                reviewed_time=NOW(),
                is_deleted='N'
        """), params)

    def validate_review(self, proposal_id: int):
        # Per-member: every active member must have at least one review/ranking before
        # finalising — symmetric with validate_feedback. A weaker "COUNT(*) > 0" check
        # let a member added mid-workflow (proposal already at REVIEW_APPROVAL) reach
        # finalisation with no review of their own.
        members = self._rows(self.db.execute(text("""
            SELECT committee_proposal_member_id
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND member_status NOT IN ('DISCARDED','REJECTED','NOT_SELECTED')
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all())

        if not members:
            raise ValueError("At least one committee review/ranking is required before finalising")

        reviewed = {r["committee_proposal_member_id"] for r in self.db.execute(text("""
            SELECT DISTINCT committee_proposal_member_id
            FROM pa_track.committee_review
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all()}

        missing = [m["committee_proposal_member_id"] for m in members if m["committee_proposal_member_id"] not in reviewed]
        if missing:
            raise ValueError(
                "Every member must be reviewed/ranked before finalising. Missing review for member(s): "
                + ", ".join(str(x) for x in missing)
            )

    def discard_members(self, proposal_id: int):
        self.db.execute(text("""
            UPDATE pa_track.committee_proposal_member
            SET member_status='DISCARDED',
                is_selected='N',
                updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id})

    def finalise_members(self, proposal_id: int, selected_ids: list[int], enable_sync: bool, user_id: int, user_name: str | None):
        if not selected_ids:
            raise ValueError("At least one selected member is required")

        selected_rows = self._rows(self.db.execute(text("""
            SELECT m.committee_proposal_member_id,
                   m.committee_proposal_position_id,
                   p.seats_required
            FROM pa_track.committee_proposal_member m
            JOIN pa_track.committee_proposal_position p
              ON p.committee_proposal_position_id=m.committee_proposal_position_id
            WHERE m.committee_proposal_id=:proposal_id
              AND m.committee_proposal_member_id IN :selected_ids
              AND m.is_deleted='N'
        """), {"proposal_id": proposal_id, "selected_ids": tuple(selected_ids)}).mappings().all())

        if len(selected_rows) != len(set(selected_ids)):
            raise ValueError("One or more selected members are invalid for this proposal")

        # capacity validation per position
        by_position = {}
        for row in selected_rows:
            by_position.setdefault(row["committee_proposal_position_id"], {"count": 0, "seats": row["seats_required"]})
            by_position[row["committee_proposal_position_id"]]["count"] += 1

        for position_id, item in by_position.items():
            if item["count"] > item["seats"]:
                raise ValueError(f"Selected members exceed seat capacity for position {position_id}: selected={item['count']}, seats={item['seats']}")

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal_member
            SET member_status='NOT_SELECTED',
                is_selected='N',
                updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id})

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal_member
            SET member_status='SELECTED',
                is_selected='Y',
                updated_by=:updated_by,
                updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
              AND committee_proposal_member_id IN :selected_ids
              AND is_deleted='N'
        """), {"proposal_id": proposal_id, "selected_ids": tuple(selected_ids), "updated_by": user_id})

        # Idempotent on the unique key uk_cfinal_proposal (committee_proposal_id, is_deleted):
        # a re-finalisation of an already-finalised committee updates the existing row
        # instead of failing with a duplicate-key (409) error.
        self.db.execute(text("""
            INSERT INTO pa_track.committee_finalization (
                committee_proposal_id, finalized_by, finalized_by_name, legacy_sync_status
            )
            VALUES (:proposal_id, :user_id, :user_name, :legacy_sync_status)
            ON DUPLICATE KEY UPDATE
                finalized_by=VALUES(finalized_by),
                finalized_by_name=VALUES(finalized_by_name),
                finalized_time=NOW(),
                legacy_sync_status=VALUES(legacy_sync_status)
        """), {
            "proposal_id": proposal_id,
            "user_id": user_id,
            "user_name": user_name,
            "legacy_sync_status": "PENDING" if enable_sync else "SKIPPED"
        })

        if enable_sync:
            self.db.execute(text("""
                INSERT INTO pa_track.committee_legacy_sync_log (
                    committee_proposal_id, sync_type, sync_status, request_json
                )
                VALUES (:proposal_id, 'COMMITTEE_FINALIZATION', 'PENDING', CAST(:payload AS JSON))
            """), {
                "proposal_id": proposal_id,
                "payload": json.dumps({"selectedMemberIds": selected_ids})
            })

    # ---------- Utility / validation ----------

    def _require_proposal(self, proposal_id: int):
        proposal = self.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Committee proposal not found: {proposal_id}")
        return proposal

    def _find_active_duplicate_proposal(self, req):
        """Return the id of an existing active (non COMPLETED/REJECTED) committee
        proposal for the same type + legacy ids **and the same set of roles**, or
        None. Used so create_proposal can redirect to the existing one instead of
        rejecting a duplicate.

        The role set is part of the identity: a committee can hold several
        distinct roles that share a display name (e.g. ST Cell has "President",
        "President ST Plain" and "President ST Terain"), and they differ only by
        legacy_tdp_committee_role_id. Matching on the committee alone would hand
        back the proposal opened for a *different* role."""
        rows = self.db.execute(text("""
            SELECT committee_proposal_id
            FROM pa_track.committee_proposal
            WHERE committee_type_code=:committee_type_code
              AND COALESCE(legacy_tdp_committee_id,0)=COALESCE(:legacy_tdp_committee_id,0)
              AND COALESCE(legacy_tdp_basic_committee_id,0)=COALESCE(:legacy_tdp_basic_committee_id,0)
              AND COALESCE(legacy_tdp_committee_level_id,0)=COALESCE(:legacy_tdp_committee_level_id,0)
              AND COALESCE(legacy_tdp_committee_level_value,0)=COALESCE(:legacy_tdp_committee_level_value,0)
              AND current_status_code NOT IN ('COMPLETED','REJECTED')
              AND is_deleted='N'
            ORDER BY committee_proposal_id
        """), {
            "committee_type_code": req.committeeTypeCode,
            "legacy_tdp_committee_id": req.legacyTdpCommitteeId,
            "legacy_tdp_basic_committee_id": req.legacyTdpBasicCommitteeId,
            "legacy_tdp_committee_level_id": req.legacyTdpCommitteeLevelId,
            "legacy_tdp_committee_level_value": req.legacyTdpCommitteeLevelValue
        }).mappings().all()
        if not rows:
            return None

        requested_roles = {p.legacyTdpCommitteeRoleId for p in req.positions}
        for row in rows:
            if self._proposal_role_ids(row["committee_proposal_id"]) == requested_roles:
                return dict(row)
        return None

    def _proposal_role_ids(self, proposal_id: int) -> set:
        """Active legacy_tdp_committee_role_id set for a proposal's positions."""
        rows = self.db.execute(text("""
            SELECT legacy_tdp_committee_role_id
            FROM pa_track.committee_proposal_position
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all()
        return {r["legacy_tdp_committee_role_id"] for r in rows}

    def _proposal_member_ids(self, proposal_id: int) -> set:
        """All active member ids for a proposal — replaces per-member existence checks."""
        rows = self.db.execute(text("""
            SELECT committee_proposal_member_id
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all()
        return {r["committee_proposal_member_id"] for r in rows}

    def _proposal_position_ids(self, proposal_id: int) -> set:
        """All active position ids for a proposal — replaces per-candidate position checks."""
        rows = self.db.execute(text("""
            SELECT committee_proposal_position_id
            FROM pa_track.committee_proposal_position
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all()
        return {r["committee_proposal_position_id"] for r in rows}

    def _existing_member_pairs(self, proposal_id: int) -> set:
        """Existing (position_id, tdp_cadre_id) pairs on a proposal — replaces the
        per-candidate duplicate check (position ids are unique to one proposal)."""
        rows = self.db.execute(text("""
            SELECT committee_proposal_position_id, tdp_cadre_id
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().all()
        return {(r["committee_proposal_position_id"], r["tdp_cadre_id"]) for r in rows}

    def _soft_deleted_member_ids_by_pair(self, proposal_id: int) -> dict:
        """Soft-deleted member ids keyed by (position_id, tdp_cadre_id) — lets a re-add
        reactivate the tombstone row instead of inserting a duplicate that would collide
        on uk_committee_candidate_position."""
        rows = self.db.execute(text("""
            SELECT committee_proposal_position_id, tdp_cadre_id, committee_proposal_member_id
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND is_deleted='Y'
        """), {"proposal_id": proposal_id}).mappings().all()
        return {(r["committee_proposal_position_id"], r["tdp_cadre_id"]): r["committee_proposal_member_id"] for r in rows}

    def _feedback_types_by_code(self, workflow_id: int) -> dict:
        """Feedback-type rows keyed by feedback_code — replaces per-feedback lookups."""
        rows = self.db.execute(text("""
            SELECT * FROM pa_track.workflow_feedback_type_master
            WHERE workflow_id=:workflow_id
              AND is_active='Y'
              AND is_deleted='N'
        """), {"workflow_id": workflow_id}).mappings().all()
        return {r["feedback_code"]: dict(r) for r in rows}

    def _roles_by_code(self) -> dict:
        """Active workflow roles keyed by role_code — replaces per-review role lookups."""
        rows = self.db.execute(text("""
            SELECT * FROM pa_track.workflow_role_master
            WHERE is_active='Y'
              AND is_deleted='N'
        """)).mappings().all()
        return {r["role_code"]: dict(r) for r in rows}

    def _cadre_status_map(self, tdp_cadre_ids) -> dict:
        """Legacy cadre is_deleted state keyed by tdp_cadre_id (one query). A missing
        key means the cadre does not exist; a value not in (None,'N') means inactive."""
        ids = sorted({int(i) for i in tdp_cadre_ids if i})
        if not ids:
            return {}
        rows = self.legacy_db.execute(
            text("""
                SELECT tdp_cadre_id, is_deleted
                FROM dakavara_pa.tdp_cadre
                WHERE tdp_cadre_id IN :ids
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).mappings().all()
        return {int(r["tdp_cadre_id"]): r["is_deleted"] for r in rows}

    def _validate_member_belongs_to_proposal(self, proposal_id: int, member_id: int):
        row = self._row(self.db.execute(text("""
            SELECT 1
            FROM pa_track.committee_proposal_member
            WHERE committee_proposal_id=:proposal_id
              AND committee_proposal_member_id=:member_id
              AND is_deleted='N'
            LIMIT 1
        """), {"proposal_id": proposal_id, "member_id": member_id}).mappings().first())
        if not row:
            raise ValueError(f"Invalid committeeProposalMemberId {member_id} for proposal {proposal_id}")

    def insert_history(self, inst: dict, proposal_id: int, transition: dict, role_code: str, req):
        self.db.execute(text("""
            INSERT INTO pa_track.committee_workflow_history (
                workflow_instance_id, committee_proposal_id, workflow_id,
                from_stage_id, from_stage_code, to_stage_id, to_stage_code,
                action_id, action_code, role_code, comments, remarks,
                action_by, action_by_name
            )
            VALUES (
                :iid, :proposal_id, :workflow_id,
                :from_stage_id, :from_stage_code, :to_stage_id, :to_stage_code,
                :action_id, :action_code, :role_code, :comments, :remarks,
                :action_by, :action_by_name
            )
        """), {
            "iid": inst["workflow_instance_id"],
            "proposal_id": proposal_id,
            "workflow_id": inst["workflow_id"],
            "from_stage_id": inst["current_stage_id"],
            "from_stage_code": inst["current_stage_code"],
            "to_stage_id": transition["to_stage_id"],
            "to_stage_code": transition["toStageCode"],
            "action_id": transition["action_id"],
            "action_code": transition["action_code"],
            "role_code": role_code,
            "comments": req.comments,
            "remarks": req.remarks,
            "action_by": req.actionBy,
            "action_by_name": req.actionByName
        })

    def insert_audit(self, proposal_id, action_code, from_status, to_status, comments, remarks, action_by, action_by_name):
        self.db.execute(text("""
            INSERT INTO pa_track.committee_proposal_audit (
                committee_proposal_id, action_code, from_status_code, to_status_code,
                comments, remarks, action_by, action_by_name
            )
            VALUES (
                :proposal_id, :action_code, :from_status, :to_status,
                :comments, :remarks, :action_by, :action_by_name
            )
        """), {
            "proposal_id": proposal_id,
            "action_code": action_code,
            "from_status": from_status,
            "to_status": to_status,
            "comments": comments,
            "remarks": remarks,
            "action_by": action_by,
            "action_by_name": action_by_name
        })

    def update_stage(self, proposal_id: int, instance_id: int, transition: dict, user_id: int):
        status = transition["toProposalStatusCode"] or transition["toStageCode"]
        status_row = self.get_status(status)

        self.db.execute(text("""
            UPDATE pa_track.workflow_instance
            SET current_stage_id=:stage_id,
                current_stage_code=:stage_code,
                updated_by=:updated_by,
                completed_time=CASE WHEN :terminal='Y' THEN NOW() ELSE completed_time END
            WHERE workflow_instance_id=:instance_id
        """), {
            "stage_id": transition["to_stage_id"],
            "stage_code": transition["toStageCode"],
            "updated_by": user_id,
            "terminal": "Y" if transition["toStageCode"] in ("COMPLETED","REJECTED") else "N",
            "instance_id": instance_id
        })

        self.db.execute(text("""
            UPDATE pa_track.committee_proposal
            SET current_status_id=:status_id,
                current_status_code=:status_code,
                updated_by=:updated_by,
                updated_time=NOW()
            WHERE committee_proposal_id=:proposal_id
        """), {
            "status_id": status_row["status_id"] if status_row else None,
            "status_code": status,
            "updated_by": user_id,
            "proposal_id": proposal_id
        })

    def insert_event(self, proposal_id: int, event_type: str, payload: dict):
        self.db.execute(text("""
            INSERT INTO pa_track.event_outbox (
                aggregate_type, aggregate_id, event_type, payload_json
            )
            VALUES (
                'COMMITTEE_WORKFLOW', :proposal_id, :event_type, CAST(:payload AS JSON)
            )
        """), {
            "proposal_id": proposal_id,
            "event_type": event_type,
            "payload": json.dumps(payload)
        })

    def history(self, proposal_id: int):
        return self._rows(self.db.execute(text("""
            SELECT *
            FROM pa_track.committee_workflow_history
            WHERE committee_proposal_id=:proposal_id
            ORDER BY committee_workflow_history_id
        """), {"proposal_id": proposal_id}).mappings().all())
