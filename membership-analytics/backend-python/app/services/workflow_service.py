class WorkflowService:
    def __init__(self, repo, enable_legacy_sync=False):
        self.repo = repo
        self.enable_legacy_sync = enable_legacy_sync

    def actions(self, proposal_id: int, role_code: str):
        return self.repo.allowed_actions(proposal_id, role_code)

    def history(self, proposal_id: int):
        return self.repo.history(proposal_id)

    def update_reviews(self, proposal_id: int, req):
        proposal = self.repo.get_proposal(proposal_id)
        if not proposal: raise ValueError(f"Proposal not found: {proposal_id}")
        self.repo.save_reviews(proposal_id, req.reviews, req.actionBy, req.actionByName)
        self.repo.db.commit()
        return {"proposalId": proposal_id, "saved": len(req.reviews)}

    def save_feedbacks_only(self, proposal_id: int, feedbacks, action_by: int = 0, action_by_name: str = ""):
        proposal = self.repo.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        workflow = self.repo.get_workflow(proposal["post_type_id"])
        if not workflow:
            raise ValueError("No active workflow configured")
        if not feedbacks:
            raise ValueError("No feedback items to save")
        self.repo.save_feedbacks(workflow["workflow_id"], proposal_id, feedbacks, action_by, action_by_name)
        self.repo.db.commit()
        return {"proposalId": proposal_id, "savedCount": len(feedbacks)}

    def execute_action(self, proposal_id: int, req):
        proposal = self.repo.get_proposal(proposal_id)
        if not proposal: raise ValueError(f"Proposal not found: {proposal_id}")
        workflow = self.repo.get_workflow(proposal["post_type_id"])
        if not workflow: raise ValueError("No active workflow configured")
        inst = self.repo.get_or_create_instance(proposal, workflow, req.actionBy, req.actionByName)
        trans = self.repo.get_transition(workflow["workflow_id"], inst["current_stage_id"], req.actionCode, req.roleCode)

        if req.feedbacks:
            self.repo.save_feedbacks(workflow["workflow_id"], proposal_id, req.feedbacks, req.actionBy, req.actionByName)
        if req.reviews:
            self.repo.save_reviews(proposal_id, req.reviews, req.actionBy, req.actionByName)
        if req.candidateDecisions:
            self.repo.save_candidate_decisions(proposal_id, req.candidateDecisions)
        if trans["requires_feedback_validation"] == "Y":
            self.repo.validate_feedback(workflow["workflow_id"], proposal_id)
        if trans["requires_review_validation"] == "Y":
            self.repo.validate_review(proposal_id)
        if trans["discard_existing_candidates"] == "Y":
            self.repo.discard_candidates(proposal_id)
        # Assign-for-GO pick: shortlist the chosen candidates (others -> NOT_SELECTED).
        # This is the action that LEAVES the Finalising stage — MOVE_TO_RELEASE_FOR_GO
        # once the RELEASE_FOR_GO migration is applied, MOVE_TO_GO_ISSUING before it
        # (so the flow keeps working during deploy). Only picks when candidate ids are
        # provided, so the later RELEASE_FOR_GO -> GO_ISSUING move (no ids, candidates
        # already shortlisted) stays a plain stage move.
        if req.actionCode in ("MOVE_TO_RELEASE_FOR_GO", "MOVE_TO_GO_ISSUING"):
            candidate_ids = list(req.selectedProposalCandidateIds or [])
            if req.selectedProposalCandidateId:
                sid = int(req.selectedProposalCandidateId)
                if sid and sid not in candidate_ids:
                    candidate_ids.append(sid)
            if candidate_ids:
                self.repo.pick_candidates_for_go(proposal_id, candidate_ids)

        if trans["requires_selected_candidate"] == "Y" and not self.repo.shortlisted_candidates(proposal_id):
            raise ValueError("At least one shortlisted candidate is required before this action")

        go_issued_now = False
        all_go_complete = False
        if trans["requires_go_details"] == "Y":
            if not req.goDetails:
                raise ValueError("GO details are required")

            eligible = self.repo.get_go_eligible_candidate(
                proposal_id, req.goDetails.proposalCandidateId
            )
            if not eligible:
                shortlisted = self.repo.shortlisted_candidates(proposal_id)
                if self.repo.candidate_has_go(proposal_id, req.goDetails.proposalCandidateId):
                    raise ValueError("GO was already issued for this candidate")
                raise ValueError(
                    "GO can only be issued for a shortlisted selected candidate awaiting GO"
                )

            self.repo.save_go(
                proposal_id,
                req.goDetails,
                req.actionBy,
                req.actionByName,
                "PENDING" if self.enable_legacy_sync else "SKIPPED",
            )

            self.repo.mark_selected_candidate_as_final_selected(
                proposal_id,
                req.goDetails.proposalCandidateId,
            )
            go_issued_now = True
            all_go_complete = self.repo.all_seat_candidates_have_go(proposal_id)

        self.repo.insert_history(inst, proposal_id, trans, req.roleCode, req)
        if go_issued_now and not all_go_complete:
            self.repo.insert_audit_and_event(
                proposal_id,
                inst["current_stage_code"],
                inst["current_stage_code"],
                req.actionCode,
                req,
            )
            self.repo.db.commit()
            seats = self.repo.seat_candidates_for_go(proposal_id)
            pending_go = sum(
                1 for row in seats
                if not self.repo.candidate_has_go(proposal_id, int(row["proposal_candidate_id"]))
            )
            return {
                "proposalId": proposal_id,
                "fromStage": inst["current_stage_code"],
                "toStage": inst["current_stage_code"],
                "actionCode": req.actionCode,
                "goIssuedForCandidateId": req.goDetails.proposalCandidateId,
                "pendingGoCount": pending_go,
                "allGoIssued": False,
            }

        to_stage = trans["toStageCode"]
        if go_issued_now and all_go_complete:
            to_stage = trans["toStageCode"]
        self.repo.insert_audit_and_event(proposal_id, inst["current_stage_code"], to_stage, req.actionCode, req)
        self.repo.update_stage(proposal_id, inst["workflow_instance_id"], trans, req.actionBy)
        self.repo.db.commit()
        return {
            "proposalId": proposal_id,
            "fromStage": inst["current_stage_code"],
            "toStage": to_stage,
            "actionCode": req.actionCode,
            "goIssuedForCandidateId": req.goDetails.proposalCandidateId if go_issued_now else None,
            "allGoIssued": all_go_complete if go_issued_now else None,
        }
