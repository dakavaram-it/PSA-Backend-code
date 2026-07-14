import logging
from repositories.committee_repository import CommitteeRepository
from utils.cadre_photo import filter_valid_mids

logger = logging.getLogger(__name__)


class CommitteeService:
    def __init__(self, repo: CommitteeRepository, enable_committee_legacy_sync: bool = False,
                 enable_committee_kss_check: bool = True):
        self.repo = repo
        self.enable_committee_legacy_sync = enable_committee_legacy_sync
        self.enable_committee_kss_check = enable_committee_kss_check

    # ---------- Master data (live from dakavara_pa) ----------

    def basic_committees(self, committee_type_id: int):
        logger.info("committee basic_committees committeeTypeId=%s", committee_type_id)
        return self.repo.get_basic_committees(committee_type_id)

    def committees(self, level_id: int, enrollment_id: int, level_value: int | None,
                   basic_committee_id: int | None = None, committee_type_id: int | None = None):
        logger.info(
            "committee committees levelId=%s enrollmentId=%s levelValue=%s basicCommitteeId=%s committeeTypeId=%s",
            level_id, enrollment_id, level_value, basic_committee_id, committee_type_id,
        )
        return self.repo.get_committees(level_id, enrollment_id, level_value, basic_committee_id, committee_type_id)

    def committee_roles(self, tdp_committee_id: int):
        logger.info("committee roles tdpCommitteeId=%s", tdp_committee_id)
        return self.repo.get_committee_roles(tdp_committee_id)

    def check_kss(self, membership_id: str):
        if not self.enable_committee_kss_check:
            # KSS gate disabled (ENABLE_COMMITTEE_KSS_CHECK=false): report the
            # candidate as eligible so the add flow proceeds without the check.
            logger.info("committee kss check skipped (disabled) mid=%s", membership_id)
            mid = (membership_id or "").strip().lstrip("#")
            return {"membershipId": mid, "isKss": True, "skipped": True, "memberships": []}
        logger.info("committee kss check mid=%s", membership_id)
        return self.repo.check_kss_membership(membership_id)

    def compare_candidates(self, proposal_id: int, mids, cadre_performance):
        """Profile/performance compare for committee candidates. MID-based — defaults
        to the committee's active member MIDs when none are supplied. Mirrors the
        Nominated Post compare but reads from the committee tables."""
        if not cadre_performance or not cadre_performance.is_configured():
            raise ValueError("report_ratings database is not configured")

        proposal = self.repo.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Committee proposal not found: {proposal_id}")

        members = proposal.get("members") or []
        member_mids = [
            m.get("membership_id") or m.get("membershipId")
            for m in members
            if (m.get("membership_id") or m.get("membershipId"))
        ]

        requested_mids = filter_valid_mids(mids)
        compare_mids = requested_mids if requested_mids else member_mids
        if not compare_mids:
            raise ValueError(
                "No candidates with valid membership IDs found on this committee. "
                "Add members before comparing."
            )

        result = cadre_performance.compare_candidates(compare_mids, members)
        result["committeeProposalId"] = proposal_id
        return result

    def create_proposal(self, req):
        logger.info("committee create proposal committeeType=%s user=%s", req.committeeTypeCode, req.createdBy)
        return self.repo.create_proposal(req)

    def delete_proposal(self, proposal_id: int, action_by: int = 0, action_by_name: str = ""):
        logger.info("committee delete proposal proposal_id=%s user=%s", proposal_id, action_by)
        return self.repo.delete_proposal(proposal_id, action_by, action_by_name)

    def add_members(self, proposal_id: int, req):
        logger.info("committee add members proposal_id=%s count=%s user=%s", proposal_id, len(req.members), req.createdBy)
        return self.repo.add_members(proposal_id, req)

    def remove_member(self, proposal_id: int, member_id: int, action_by: int = 0, action_by_name: str | None = None):
        logger.info("committee remove member proposal_id=%s member_id=%s", proposal_id, member_id)
        return self.repo.remove_member(proposal_id, member_id, action_by, action_by_name)

    def remove_all_members(self, proposal_id: int, action_by: int = 0, action_by_name: str | None = None):
        logger.info("committee remove all members proposal_id=%s", proposal_id)
        return self.repo.remove_all_members(proposal_id, action_by, action_by_name)

    def update_status(self, proposal_id: int, req):
        logger.info("committee update status proposal_id=%s status=%s", proposal_id, req.statusCode)
        return self.repo.update_status(proposal_id, req.statusCode, req.actionBy, req.actionByName, req.remarks)

    def get_proposal(self, proposal_id: int, cadre_performance=None):
        proposal = self.repo.get_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Committee proposal not found: {proposal_id}")
        # Merge the report_ratings profile (correct caste / assembly / parliament,
        # refreshed via the performance procedures) over the raw dakavara values the
        # repo attached, so committee member cards match Nominated Post.
        members = proposal.get("members") or []
        if members and cadre_performance and cadre_performance.is_configured():
            proposal["members"] = cadre_performance.attach_profiles_to_candidates(members)
        return proposal

    def list_proposals(self, limit: int = 200, status_code: str | None = None):
        logger.info("committee list proposals limit=%s statusCode=%s", limit, status_code)
        return self.repo.list_proposals(limit, status_code)

    def actions(self, proposal_id: int, role_code: str):
        return self.repo.allowed_actions(proposal_id, role_code)

    def execute_action(self, proposal_id: int, req):
        return self.repo.execute_action(
            proposal_id=proposal_id,
            req=req,
            enable_committee_legacy_sync=self.enable_committee_legacy_sync
        )

    def save_feedbacks_only(self, proposal_id: int, feedbacks, action_by: int, action_by_name: str | None):
        logger.info("committee save feedbacks only proposal_id=%s count=%s", proposal_id, len(feedbacks or []))
        return self.repo.save_feedbacks_only(proposal_id, feedbacks, action_by, action_by_name)

    def history(self, proposal_id: int):
        return self.repo.history(proposal_id)
