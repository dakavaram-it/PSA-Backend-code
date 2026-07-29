import json
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

class WorkflowRepository:
    def __init__(self, pa_track_db: Session):
        self.db = pa_track_db

    @staticmethod
    def _row(row): return dict(row) if row else None
    @staticmethod
    def _rows(rows): return [dict(r) for r in rows]

    def get_proposal(self, proposal_id: int):
        return self._row(self.db.execute(text("""
            SELECT * FROM nominated_post_proposal WHERE proposal_id=:proposal_id AND is_deleted='N'
        """), {"proposal_id": proposal_id}).mappings().first())

    def get_workflow(self, post_type_id: int):
        return self._row(self.db.execute(text("""
            SELECT * FROM workflow_definition
            WHERE post_type_id=:post_type_id AND is_active='Y' AND is_deleted='N'
            ORDER BY version_no DESC LIMIT 1
        """), {"post_type_id": post_type_id}).mappings().first())

    def get_stage_by_code(self, workflow_id: int, stage_code: str):
        return self._row(self.db.execute(text("""
            SELECT * FROM workflow_stage_master
            WHERE workflow_id=:workflow_id AND stage_code=:stage_code AND is_active='Y' AND is_deleted='N'
        """), {"workflow_id": workflow_id, "stage_code": stage_code}).mappings().first())

    def get_initial_stage(self, workflow_id: int):
        return self._row(self.db.execute(text("""
            SELECT * FROM workflow_stage_master
            WHERE workflow_id=:workflow_id AND is_initial_stage='Y' AND is_active='Y' AND is_deleted='N'
            LIMIT 1
        """), {"workflow_id": workflow_id}).mappings().first())

    def get_role(self, role_code: str):
        return self._row(self.db.execute(text("""
            SELECT * FROM workflow_role_master WHERE role_code=:role_code AND is_active='Y' AND is_deleted='N'
        """), {"role_code": role_code}).mappings().first())

    def get_or_create_instance(self, proposal: dict, workflow: dict, user_id: int, user_name: str | None):
        inst = self._row(self.db.execute(text("""
            SELECT * FROM workflow_instance
            WHERE post_type_id=:post_type_id AND reference_table_name='nominated_post_proposal'
              AND reference_id=:proposal_id AND is_deleted='N'
        """), {"post_type_id": proposal["post_type_id"], "proposal_id": proposal["proposal_id"]}).mappings().first())
        if inst: return inst
        stage = self.get_stage_by_code(workflow["workflow_id"], proposal["current_status_code"]) or self.get_initial_stage(workflow["workflow_id"])
        self.db.execute(text("""
            INSERT INTO workflow_instance (
                workflow_id, post_type_id, reference_table_name, reference_id,
                current_stage_id, current_stage_code, started_by, started_by_name, inserted_by
            ) VALUES (
                :workflow_id, :post_type_id, 'nominated_post_proposal', :proposal_id,
                :stage_id, :stage_code, :started_by, :started_by_name, :inserted_by
            )
        """), {
            "workflow_id": workflow["workflow_id"], "post_type_id": proposal["post_type_id"],
            "proposal_id": proposal["proposal_id"], "stage_id": stage["workflow_stage_id"],
            "stage_code": stage["stage_code"], "started_by": user_id,
            "started_by_name": user_name, "inserted_by": user_id
        })
        iid = self.db.execute(text("SELECT LAST_INSERT_ID() id")).mappings().first()["id"]
        return self._row(self.db.execute(text("SELECT * FROM workflow_instance WHERE workflow_instance_id=:id"), {"id": iid}).mappings().first())

    def get_transition(self, workflow_id: int, from_stage_id: int, action_code: str, role_code: str):
        role = self.get_role(role_code)
        if not role: raise ValueError(f"Invalid roleCode: {role_code}")
        trans = self._row(self.db.execute(text("""
            SELECT t.*, a.action_code, fs.stage_code fromStageCode, ts.stage_code toStageCode,
                   ts.mapped_proposal_status_code toProposalStatusCode
            FROM workflow_transition_master t
            JOIN workflow_action_master a ON t.action_id=a.workflow_action_id
            JOIN workflow_stage_master fs ON t.from_stage_id=fs.workflow_stage_id
            JOIN workflow_stage_master ts ON t.to_stage_id=ts.workflow_stage_id
            WHERE t.workflow_id=:workflow_id AND t.from_stage_id=:from_stage_id
              AND a.action_code=:action_code AND t.is_active='Y' AND t.is_deleted='N'
        """), {"workflow_id": workflow_id, "from_stage_id": from_stage_id, "action_code": action_code}).mappings().first())
        if not trans: raise ValueError(f"Action {action_code} is not allowed from current stage")
        if role["is_admin_role"] == "Y": return trans
        allowed = self._row(self.db.execute(text("""
            SELECT 1 FROM workflow_stage_role_mapping
            WHERE workflow_id=:workflow_id AND stage_id=:stage_id AND role_id=:role_id
              AND (action_id IS NULL OR action_id=:action_id)
              AND can_act='Y' AND is_active='Y' AND is_deleted='N' LIMIT 1
        """), {"workflow_id": workflow_id, "stage_id": from_stage_id, "role_id": role["workflow_role_id"], "action_id": trans["action_id"]}).mappings().first())
        if not allowed: raise ValueError(f"Role {role_code} cannot perform {action_code}")
        return trans

    def allowed_actions(self, proposal_id: int, role_code: str):
        proposal = self.get_proposal(proposal_id)
        if not proposal: raise ValueError("Proposal not found")
        workflow = self.get_workflow(proposal["post_type_id"])
        inst = self.get_or_create_instance(proposal, workflow, proposal["created_by"], proposal.get("created_by_name"))
        role = self.get_role(role_code)
        if not role: raise ValueError(f"Invalid roleCode: {role_code}")
        rows = self.db.execute(text("""
            SELECT a.action_code actionCode, a.action_name actionName, a.button_label buttonLabel,
                   ts.stage_code toStageCode, ts.stage_name toStageName
            FROM workflow_transition_master t
            JOIN workflow_action_master a ON t.action_id=a.workflow_action_id
            JOIN workflow_stage_master ts ON t.to_stage_id=ts.workflow_stage_id
            WHERE t.workflow_id=:workflow_id AND t.from_stage_id=:stage_id
              AND t.is_active='Y' AND t.is_deleted='N'
            ORDER BY t.display_order
        """), {"workflow_id": workflow["workflow_id"], "stage_id": inst["current_stage_id"]}).mappings().all()
        return {"proposalId": proposal_id, "currentStageCode": inst["current_stage_code"], "roleCode": role_code, "allowedActions": self._rows(rows)}

    def save_feedbacks(self, workflow_id: int, proposal_id: int, feedbacks: list, user_id: int, user_name: str | None):
        # Soft-delete existing rows so re-saves after reverting don't create duplicates
        self.db.execute(text("""
            UPDATE proposal_feedback SET is_deleted='Y'
            WHERE proposal_id=:proposal_id AND is_deleted='N'
        """), {"proposal_id": proposal_id})
        for fb in feedbacks:
            ft = self._row(self.db.execute(text("""
                SELECT * FROM workflow_feedback_type_master
                WHERE workflow_id=:workflow_id AND feedback_code=:feedback_code AND is_active='Y' AND is_deleted='N'
            """), {"workflow_id": workflow_id, "feedback_code": fb.feedbackCode}).mappings().first())
            if not ft: raise ValueError(f"Invalid feedbackCode: {fb.feedbackCode}")
            params = {
                "proposal_id": proposal_id,
                "proposal_candidate_id": fb.proposalCandidateId,
                "feedback_type_id": ft["feedback_type_id"],
                "feedback_code": fb.feedbackCode,
                "feedback_text": fb.feedbackText,
                "no_feedback_required": fb.noFeedbackRequired,
                "submitted_by": user_id,
                "submitted_by_name": user_name,
            }
            existing = self._row(self.db.execute(text("""
                SELECT proposal_feedback_id FROM proposal_feedback
                WHERE proposal_id=:proposal_id
                  AND feedback_type_id=:feedback_type_id
                  AND is_deleted='N'
                  AND proposal_candidate_id <=> :proposal_candidate_id
                LIMIT 1
            """), params).mappings().first())
            if existing:
                self.db.execute(text("""
                    UPDATE proposal_feedback SET
                        feedback_text=:feedback_text,
                        no_feedback_required=:no_feedback_required,
                        submitted_by=:submitted_by,
                        submitted_by_name=:submitted_by_name,
                        submitted_time=NOW()
                    WHERE proposal_feedback_id=:proposal_feedback_id
                """), {**params, "proposal_feedback_id": existing["proposal_feedback_id"]})
            else:
                self.db.execute(text("""
                    INSERT INTO proposal_feedback (
                        proposal_id, proposal_candidate_id, feedback_type_id, feedback_code,
                        feedback_text, no_feedback_required, submitted_by, submitted_by_name
                    ) VALUES (
                        :proposal_id, :proposal_candidate_id, :feedback_type_id, :feedback_code,
                        :feedback_text, :no_feedback_required, :submitted_by, :submitted_by_name
                    )
                """), params)

    def validate_feedback(self, workflow_id: int, proposal_id: int):

        mandatory_feedbacks = self._rows(
            self.db.execute(
                text("""
                    SELECT feedback_code
                    FROM workflow_feedback_type_master
                    WHERE workflow_id=:workflow_id
                    AND is_mandatory='Y'
                    AND is_active='Y'
                    AND is_deleted='N'
                """),
                {"workflow_id": workflow_id}
            ).mappings().all()
        )

        mandatory_codes = {
            x["feedback_code"]
            for x in mandatory_feedbacks
        }

        candidates = self._rows(
            self.db.execute(
                text("""
                    SELECT proposal_candidate_id
                    FROM nominated_post_proposal_candidate
                    WHERE proposal_id=:proposal_id
                    AND is_deleted='N'
                """),
                {"proposal_id": proposal_id}
            ).mappings().all()
        )

        errors = []

        for candidate in candidates:

            candidate_id = candidate["proposal_candidate_id"]

            feedbacks = self._rows(
                self.db.execute(
                    text("""
                        SELECT
                            feedback_code,
                            no_feedback_required
                        FROM proposal_feedback
                        WHERE proposal_id=:proposal_id
                        AND proposal_candidate_id=:candidate_id
                        AND is_deleted='N'
                    """),
                    {
                        "proposal_id": proposal_id,
                        "candidate_id": candidate_id
                    }
                ).mappings().all()
            )

            if not feedbacks:
                errors.append(
                    f"Candidate {candidate_id} has no feedback"
                )
                continue

            if any(
                x["no_feedback_required"] == "Y"
                for x in feedbacks
            ):
                continue

            candidate_codes = {
                x["feedback_code"]
                for x in feedbacks
            }

            missing = mandatory_codes - candidate_codes

            if missing:
                errors.append(
                    f"Candidate {candidate_id} missing feedbacks: {', '.join(missing)}"
                )

        if errors:
            raise ValueError("; ".join(errors))

    def save_reviews(self, proposal_id: int, reviews: list, user_id: int, user_name: str | None):
        for rv in reviews:
            role = self.get_role(rv.reviewerRoleCode)
            if not role: raise ValueError(f"Invalid reviewerRoleCode: {rv.reviewerRoleCode}")
            self.db.execute(text("""
                INSERT INTO proposal_review (
                    proposal_id, proposal_candidate_id, reviewer_role_id, reviewer_role_code,
                    reviewer_label, rank_value, review_status, review_comments, reviewed_by, reviewed_by_name
                ) VALUES (
                    :proposal_id, :candidate_id, :role_id, :role_code,
                    :role_label, :rank_value, :review_status, :comments, :user_id, :user_name
                )
                ON DUPLICATE KEY UPDATE rank_value=VALUES(rank_value), review_status=VALUES(review_status),
                    review_comments=VALUES(review_comments), reviewed_by=VALUES(reviewed_by),
                    reviewed_by_name=VALUES(reviewed_by_name), reviewed_time=NOW(), is_deleted='N'
            """), {"proposal_id": proposal_id, "candidate_id": rv.proposalCandidateId,
                    "role_id": role["workflow_role_id"], "role_code": role["role_code"], "role_label": role["role_label"],
                    "rank_value": rv.rankValue, "review_status": rv.reviewStatus, "comments": rv.reviewComments,
                    "user_id": user_id, "user_name": user_name})

    def validate_review(self, proposal_id: int):
        row = self._row(self.db.execute(text("SELECT COUNT(*) cnt FROM proposal_review WHERE proposal_id=:id AND is_deleted='N'"), {"id": proposal_id}).mappings().first())
        if int(row["cnt"] or 0) == 0: raise ValueError("At least one review/ranking is required before finalising")

    def save_candidate_decisions(self, proposal_id: int, decisions: list):
        """Persist UI stage decisions (On Hold / Shortlisted / Rejected) to candidate_status."""
        allowed = {'SHORTLISTED', 'ON_HOLD', 'REJECTED', 'NOT_SELECTED', 'ADDED', 'SELECTED'}
        for d in decisions:
            status = (d.status or '').upper()
            if status not in allowed:
                continue
            self.db.execute(text("""
                UPDATE nominated_post_proposal_candidate
                SET candidate_status = :status
                WHERE proposal_id = :proposal_id
                  AND proposal_candidate_id = :candidate_id
                  AND is_deleted = 'N'
            """), {"status": status, "proposal_id": proposal_id, "candidate_id": d.proposalCandidateId})

    def discard_candidates(self, proposal_id: int):
        self.db.execute(text("UPDATE nominated_post_proposal_candidate SET candidate_status='DISCARDED', is_selected='N' WHERE proposal_id=:id AND is_deleted='N'"), {"id": proposal_id})

    def pick_candidate(self, proposal_id: int, candidate_id: int):
        """Backward-compatible single-candidate pick."""
        self.pick_candidates_for_go(proposal_id, [candidate_id])

    def pick_candidates_for_go(self, proposal_id: int, candidate_ids: list[int]):
        """
        Pick one or more candidates for seat(s) before GO issuing.
        Each picked candidate -> SHORTLISTED; others -> NOT_SELECTED.
        """
        unique_ids = []
        seen = set()
        for cid in candidate_ids:
            cid = int(cid)
            if cid and cid not in seen:
                seen.add(cid)
                unique_ids.append(cid)
        if not unique_ids:
            raise ValueError("At least one candidate must be selected for GO issuing")

        rows = self._rows(self.db.execute(text("""
            SELECT proposal_candidate_id
            FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
              AND proposal_candidate_id IN :candidate_ids
              AND is_deleted = 'N'
        """).bindparams(bindparam("candidate_ids", expanding=True)), {
            "proposal_id": proposal_id,
            "candidate_ids": unique_ids,
        }).mappings().all())
        found = {int(r["proposal_candidate_id"]) for r in rows}
        missing = [cid for cid in unique_ids if cid not in found]
        if missing:
            raise ValueError(
                f"Selected candidate(s) not found in proposal. proposalId={proposal_id}, missing={missing}"
            )

        self.db.execute(text("""
            UPDATE nominated_post_proposal_candidate
            SET is_selected = 'N',
                candidate_status = 'NOT_SELECTED',
                updated_time = NOW()
            WHERE proposal_id = :proposal_id
              AND is_deleted = 'N'
        """), {"proposal_id": proposal_id})

        self.db.execute(text("""
            UPDATE nominated_post_proposal_candidate
            SET is_selected = 'Y',
                candidate_status = 'SHORTLISTED',
                updated_time = NOW()
            WHERE proposal_id = :proposal_id
              AND proposal_candidate_id IN :candidate_ids
              AND is_deleted = 'N'
        """).bindparams(bindparam("candidate_ids", expanding=True)), {
            "proposal_id": proposal_id,
            "candidate_ids": unique_ids,
        })

    def shortlisted_candidates(self, proposal_id: int):
        return self._rows(self.db.execute(text("""
            SELECT *
            FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
              AND is_selected = 'Y'
              AND candidate_status = 'SHORTLISTED'
              AND is_deleted = 'N'
            ORDER BY proposal_candidate_id
        """), {"proposal_id": proposal_id}).mappings().all())

    def selected_candidate(self, proposal_id: int):
        """First shortlisted candidate (legacy callers)."""
        rows = self.shortlisted_candidates(proposal_id)
        return rows[0] if rows else None

    def get_go_eligible_candidate(self, proposal_id: int, proposal_candidate_id: int):
        return self._row(self.db.execute(text("""
            SELECT *
            FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
              AND proposal_candidate_id = :proposal_candidate_id
              AND is_selected = 'Y'
              AND candidate_status = 'SHORTLISTED'
              AND is_deleted = 'N'
        """), {
            "proposal_id": proposal_id,
            "proposal_candidate_id": proposal_candidate_id,
        }).mappings().first())

    def get_go_details_for_proposal(self, proposal_id: int):
        return self._rows(self.db.execute(text("""
            SELECT
                proposal_go_id AS proposalGoId,
                proposal_id AS proposalId,
                proposal_candidate_id AS proposalCandidateId,
                go_number AS goNumber,
                go_issue_date AS goIssueDate,
                go_remarks AS goRemarks,
                legacy_sync_status AS legacySyncStatus,
                issued_by AS issuedBy,
                issued_by_name AS issuedByName
            FROM proposal_go_details
            WHERE proposal_id = :proposal_id
              AND is_deleted = 'N'
            ORDER BY proposal_go_id
        """), {"proposal_id": proposal_id}).mappings().all())

    def candidate_has_go(self, proposal_id: int, proposal_candidate_id: int) -> bool:
        row = self._row(self.db.execute(text("""
            SELECT 1 AS ok
            FROM proposal_go_details
            WHERE proposal_id = :proposal_id
              AND proposal_candidate_id = :proposal_candidate_id
              AND is_deleted = 'N'
            LIMIT 1
        """), {
            "proposal_id": proposal_id,
            "proposal_candidate_id": proposal_candidate_id,
        }).mappings().first())
        return row is not None

    def seat_candidates_for_go(self, proposal_id: int):
        """Candidates picked for seat(s): shortlisted and/or already GO-issued (SELECTED)."""
        return self._rows(self.db.execute(text("""
            SELECT *
            FROM nominated_post_proposal_candidate
            WHERE proposal_id = :proposal_id
              AND is_selected = 'Y'
              AND candidate_status IN ('SHORTLISTED', 'SELECTED')
              AND is_deleted = 'N'
            ORDER BY proposal_candidate_id
        """), {"proposal_id": proposal_id}).mappings().all())

    def all_seat_candidates_have_go(self, proposal_id: int) -> bool:
        seats = self.seat_candidates_for_go(proposal_id)
        if not seats:
            return False
        for row in seats:
            if not self.candidate_has_go(proposal_id, int(row["proposal_candidate_id"])):
                return False
        return True
    
    def mark_selected_candidate_as_final_selected(self, proposal_id: int, proposal_candidate_id: int):
        """
        Issue GO:
        shortlisted candidate -> SELECTED
        """
        result = self.db.execute(text("""
            UPDATE nominated_post_proposal_candidate
            SET candidate_status = 'SELECTED',
                is_selected = 'Y',
                updated_time = NOW()
            WHERE proposal_id = :proposal_id
            AND proposal_candidate_id = :proposal_candidate_id
            AND candidate_status = 'SHORTLISTED'
            AND is_selected = 'Y'
            AND is_deleted = 'N'
        """), {
            "proposal_id": proposal_id,
            "proposal_candidate_id": proposal_candidate_id,
        })

        if result.rowcount == 0:
            raise ValueError("GO can be issued only for the shortlisted selected candidate")

    def save_go(self, proposal_id: int, go, user_id: int, user_name: str | None, legacy_status: str):
        existing = self._row(self.db.execute(text("""
            SELECT proposal_go_id
            FROM proposal_go_details
            WHERE proposal_id = :pid
              AND proposal_candidate_id = :cid
              AND is_deleted = 'N'
            LIMIT 1
        """), {"pid": proposal_id, "cid": go.proposalCandidateId}).mappings().first())
        params = {
            "pid": proposal_id,
            "cid": go.proposalCandidateId,
            "go_number": go.goNumber,
            "go_date": go.goIssueDate,
            "remarks": go.goRemarks,
            "legacy_status": legacy_status,
            "uid": user_id,
            "uname": user_name,
        }
        if existing:
            self.db.execute(text("""
                UPDATE proposal_go_details
                SET go_number = :go_number,
                    go_issue_date = :go_date,
                    go_remarks = :remarks,
                    legacy_sync_status = :legacy_status,
                    issued_by = :uid,
                    issued_by_name = :uname
                WHERE proposal_go_id = :go_id
            """), {**params, "go_id": existing["proposal_go_id"]})
        else:
            self.db.execute(text("""
                INSERT INTO proposal_go_details (
                    proposal_id, proposal_candidate_id, go_number, go_issue_date,
                    go_remarks, legacy_sync_status, issued_by, issued_by_name
                ) VALUES (
                    :pid, :cid, :go_number, :go_date, :remarks, :legacy_status, :uid, :uname
                )
            """), params)

    def update_stage(self, proposal_id: int, inst_id: int, transition: dict, user_id: int):
        to_stage = self._row(self.db.execute(text("SELECT * FROM workflow_stage_master WHERE workflow_stage_id=:id"), {"id": transition["to_stage_id"]}).mappings().first())
        self.db.execute(text("UPDATE workflow_instance SET current_stage_id=:sid, current_stage_code=:scode, completed_time=CASE WHEN :terminal='Y' THEN NOW() ELSE completed_time END, updated_by=:uid WHERE workflow_instance_id=:iid"),
                        {"sid": to_stage["workflow_stage_id"], "scode": to_stage["stage_code"], "terminal": to_stage["is_terminal_stage"], "uid": user_id, "iid": inst_id})
        status = to_stage["mapped_proposal_status_code"] or to_stage["stage_code"]
        st = self._row(self.db.execute(text("SELECT status_id FROM proposal_status_master WHERE status_code=:code AND is_deleted='N'"), {"code": status}).mappings().first())
        if st:
            self.db.execute(text("UPDATE nominated_post_proposal SET current_status_id=:sid, current_status_code=:scode, updated_by=:uid, updated_time=NOW() WHERE proposal_id=:pid"),
                            {"sid": st["status_id"], "scode": status, "uid": user_id, "pid": proposal_id})

    def insert_history(self, inst: dict, proposal_id: int, transition: dict, role_code: str, req):
        self.db.execute(text("""
            INSERT INTO proposal_workflow_history (
                workflow_instance_id, proposal_id, workflow_id, from_stage_id, from_stage_code,
                to_stage_id, to_stage_code, action_id, action_code, role_code,
                comments, remarks, action_by, action_by_name
            ) VALUES (
                :iid, :pid, :wid, :fsid, :fscode, :tsid, :tscode, :aid, :acode, :role,
                :comments, :remarks, :uid, :uname
            )
        """), {"iid": inst["workflow_instance_id"], "pid": proposal_id, "wid": inst["workflow_id"],
                "fsid": inst["current_stage_id"], "fscode": inst["current_stage_code"],
                "tsid": transition["to_stage_id"], "tscode": transition["toStageCode"], "aid": transition["action_id"],
                "acode": transition["action_code"], "role": role_code, "comments": req.comments,
                "remarks": req.remarks, "uid": req.actionBy, "uname": req.actionByName})

    def insert_audit_and_event(self, proposal_id: int, from_stage: str, to_stage: str, action_code: str, req):
        self.db.execute(text("""
            INSERT INTO nominated_post_proposal_audit (proposal_id, action_code, from_status_code, to_status_code, comments, remarks, action_by, action_by_name)
            VALUES (:pid, :acode, :from_s, :to_s, :comments, :remarks, :uid, :uname)
        """), {"pid": proposal_id, "acode": action_code, "from_s": from_stage, "to_s": to_stage, "comments": req.comments, "remarks": req.remarks, "uid": req.actionBy, "uname": req.actionByName})
        self.db.execute(text("""
            INSERT INTO event_outbox (aggregate_type, aggregate_id, event_type, payload_json)
            VALUES ('NOMINATED_POST_WORKFLOW', :pid, :etype, CAST(:payload AS JSON))
        """), {"pid": proposal_id, "etype": f"workflow.{action_code.lower()}", "payload": json.dumps({"proposalId": proposal_id, "fromStage": from_stage, "toStage": to_stage, "actionCode": action_code})})

    def history(self, proposal_id: int):
        return self._rows(self.db.execute(text("SELECT * FROM proposal_workflow_history WHERE proposal_id=:id ORDER BY workflow_history_id"), {"id": proposal_id}).mappings().all())
