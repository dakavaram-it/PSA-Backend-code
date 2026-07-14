import logging
import time

from sqlalchemy.exc import IntegrityError

from repositories.nominated_proposal_repository import NominatedProposalRepository
from services.cadre_performance_service import CadrePerformanceService
from utils.cadre_photo import filter_valid_mids, normalize_mid

logger = logging.getLogger(__name__)

class NominatedProposalService:
    def __init__(
        self,
        repository: NominatedProposalRepository,
        cadre_performance_service: CadrePerformanceService | None = None,
    ):
        self.repo = repository
        self.cadre_performance = cadre_performance_service

    def create_proposal(self, req):
        started = time.time()
        capacity = self.repo.get_capacity_from_legacy(req.enrollmentId, req.boardLevelId, req.locationValue, req.departmentId, req.boardId, req.positionId, req.nominatedPostMemberId)
        if not capacity:
            raise ValueError("Invalid nominated post member/capacity combination selected")
        result = self.repo.create_proposal(req, capacity)
        logger.info("service=create_proposal success proposalId=%s elapsedMs=%s", result["proposal_id"], round((time.time()-started)*1000,2))
        return result

    def add_candidates(self, proposal_id: int, req):
        seen: set = set()
        unique = []
        for item in req.candidates:
            key = item.nominationPostCandidateId or item.tdpCadreId
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        if not unique:
            raise ValueError("No candidates to add")
        added = 0
        skipped = 0
        try:
            for item in unique:
                # Per-candidate savepoint: a duplicate that slips past the pre-check
                # (concurrent/double submit) and trips the uk_prop_cadre_active unique
                # key is treated as "skipped" instead of aborting the whole batch with
                # a 409. The savepoint rolls back just that insert; the rest still commit.
                try:
                    with self.repo.pa_track_db.begin_nested():
                        result = self.repo.add_candidate(proposal_id, item, req.createdBy, req.createdByName)
                except IntegrityError:
                    result = "skipped"
                if result == "skipped":
                    skipped += 1
                else:
                    added += 1
            if added == 0 and skipped == 0:
                raise ValueError("No candidates could be added")
            self.repo.update_proposed_count(proposal_id)
            if added > 0:
                self.repo.insert_audit(
                    proposal_id, "ADD_CANDIDATES", None, None, req.createdBy, req.createdByName,
                    f"Added {added} candidate(s)" + (f", {skipped} already present" if skipped else ""), None,
                )
                self.repo.insert_event(
                    "NOMINATED_POST_PROPOSAL", proposal_id, "proposal.candidates_added",
                    {"proposalId": proposal_id, "count": added, "skipped": skipped},
                )
            self.repo.pa_track_db.commit()
            if added > 0:
                self._sync_saved_candidate_performance(proposal_id)
        except Exception:
            self.repo.pa_track_db.rollback()
            raise
        return self.repo.get_proposal_detail(proposal_id)

    def _sync_saved_candidate_performance(self, proposal_id: int):
        if not self.cadre_performance or not CadrePerformanceService.is_configured():
            return
        mids = self.repo.list_active_candidate_mids(proposal_id)
        if not mids:
            return
        try:
            self.cadre_performance.sync_performance_update(mids)
            logger.info(
                "service=add_candidates performance_update proposalId=%s mids=%s",
                proposal_id, len(mids),
            )
        except Exception as exc:
            logger.warning(
                "service=add_candidates performance_update_failed proposalId=%s error=%s",
                proposal_id, str(exc),
            )

    def add_manual_candidate(self, proposal_id: int, req):
        """Create a brand-new candidate directly on the proposal (source_type MANUAL)."""
        try:
            new_id = self.repo.add_manual_candidate(proposal_id, req)
            self.repo.update_proposed_count(proposal_id)
            self.repo.insert_audit(
                proposal_id, "ADD_CANDIDATES", None, None, req.createdBy, req.createdByName,
                f"Created new candidate: {req.candidateName}", req.remarks,
            )
            self.repo.insert_event(
                "NOMINATED_POST_PROPOSAL", proposal_id, "proposal.candidates_added",
                {"proposalId": proposal_id, "count": 1, "manual": True, "proposalCandidateId": new_id},
            )
            self.repo.pa_track_db.commit()
        except Exception:
            self.repo.pa_track_db.rollback()
            raise
        return self.repo.get_proposal_detail(proposal_id)

    def remove_candidate(self, proposal_id: int, proposal_candidate_id: int, action_by: int = 0, action_by_name: str = ""):
        name = self.repo.remove_candidate(proposal_id, proposal_candidate_id, action_by, action_by_name)
        self.repo.update_proposed_count(proposal_id)
        self.repo.insert_audit(
            proposal_id, "REMOVE_CANDIDATE", None, None, action_by, action_by_name,
            f"Removed candidate: {name}", None,
        )
        self.repo.insert_event(
            "NOMINATED_POST_PROPOSAL", proposal_id, "proposal.candidate_removed",
            {"proposalId": proposal_id, "proposalCandidateId": proposal_candidate_id},
        )
        self.repo.pa_track_db.commit()
        return self.repo.get_proposal_detail(proposal_id)

    def remove_all_candidates(self, proposal_id: int, action_by: int = 0, action_by_name: str = ""):
        removed = self.repo.remove_all_candidates(proposal_id, action_by, action_by_name)
        self.repo.update_proposed_count(proposal_id)
        if removed > 0:
            self.repo.insert_audit(
                proposal_id, "REMOVE_ALL_CANDIDATES", None, None, action_by, action_by_name,
                f"Removed {removed} candidate(s) — fresh cycle", None,
            )
            self.repo.insert_event(
                "NOMINATED_POST_PROPOSAL", proposal_id, "proposal.candidates_cleared",
                {"proposalId": proposal_id, "count": removed},
            )
        self.repo.pa_track_db.commit()
        return self.repo.get_proposal_detail(proposal_id)

    def delete_proposal(self, proposal_id: int, action_by: int = 0, action_by_name: str = ""):
        return self.repo.delete_proposal(proposal_id, action_by, action_by_name)

    def get_proposal(self, proposal_id: int):
        result = self.repo.get_proposal_detail(proposal_id)
        if not result:
            raise ValueError(f"Proposal not found: {proposal_id}")
        candidates = result.get("candidates") or []
        if candidates and self.cadre_performance:
            result["candidates"] = self.cadre_performance.attach_profiles_to_candidates(candidates)
        return result

    def list_proposals(
        self,
        enrollment_id: int = 2,
        limit: int = 200,
        offset: int = 0,
        status_code: str | None = None,
    ):
        return self.repo.list_proposals(
            enrollment_id=enrollment_id,
            limit=limit,
            offset=offset,
            status_code=status_code,
        )

    def update_proposal_status(
        self,
        proposal_id: int,
        status_code: str,
        remarks: str | None = None,
        action_by: int = 0,
        action_by_name: str = "",
    ):
        result = self.repo.update_proposal_status(
            proposal_id, status_code, remarks, action_by, action_by_name
        )
        if not result:
            raise ValueError(f"Proposal not found: {proposal_id}")
        return result

    # supports Mobile and MID search for candidates
    def search_candidates(self, mid: str | None = None, mobile: str | None = None, limit: int = 10):
        if self.cadre_performance:
            return self.cadre_performance.search_candidates(mid=mid, mobile=mobile, limit=limit)
        return self._wrap_search_candidates(self.repo.search_cadre(mid=mid, mobile=mobile, limit=limit))

    def list_candidate_pool(self, limit: int = 500):
        """Candidate pool from report_ratings.cadre_details, ranked by performance score.
        Returns an empty pool when report_ratings enrichment isn't configured."""
        if self.cadre_performance:
            return self.cadre_performance.list_candidate_pool(limit=limit)
        return {"candidates": [], "total": 0}

    def compare_pool_candidates(self, mids: list[str] | None = None):
        """Compare selected Candidate Pool cadres from the _nom snapshot + feedback
        tables (no performance procedures). Same payload shape as the proposal compare."""
        if not self.cadre_performance:
            return {"mids": [], "candidates": [], "feedbackQuestions": []}
        return self.cadre_performance.compare_pool_candidates(mids or [])

    @staticmethod
    def _wrap_search_candidates(rows: list[dict]) -> list[dict]:
        results = []
        for row in rows or []:
            membership_id = normalize_mid(row.get("membershipId"))
            if not membership_id:
                continue
            results.append({
                "membershipId": membership_id,
                "mid": row.get("mid") or f"#{membership_id}",
                "profile": dict(row),
            })
        return results

    def get_candidate_profile_report(self, mid: str, refresh: bool = False):
        if not self.cadre_performance:
            raise ValueError("report_ratings database is not configured")
        return self.cadre_performance.get_profile_report_by_mid(mid, refresh=refresh)

    def get_caste_options(self, state_id: int = 1):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.get_caste_options(state_id)

    def update_candidate_caste(self, mid: str, caste_state_id: int):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.update_candidate_caste(mid, caste_state_id)

    def get_occupation_options(self):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.get_occupation_options()

    def update_candidate_occupation(self, mid: str, occupation_id: int):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.update_candidate_occupation(mid, occupation_id)

    def get_education_options(self):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.get_education_options()

    def get_party_options(self):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.get_party_options()

    def update_candidate_education(self, mid: str, education_id: int):
        if not self.cadre_performance:
            raise ValueError("Cadre profile service is not available")
        return self.cadre_performance.update_candidate_education(mid, education_id)

    def compare_proposal_candidates(self, proposal_id: int, mids: list[str] | None = None):
        if not self.cadre_performance:
            raise ValueError("report_ratings database is not configured")
        proposal = self.repo.get_proposal_header(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal not found: {proposal_id}")
        proposal_candidates = self.repo.list_candidates_for_proposals([proposal_id])
        proposal_mids = self.repo.list_active_candidate_mids(proposal_id)

        requested_mids = filter_valid_mids(mids)
        compare_mids = requested_mids if requested_mids else proposal_mids
        if not compare_mids:
            raise ValueError(
                "No candidates with valid membership IDs found on this proposal. "
                "Add candidates in Step 2 before comparing."
            )

        result = self.cadre_performance.compare_candidates(compare_mids, proposal_candidates)
        result["proposalId"] = proposal_id
        return result
