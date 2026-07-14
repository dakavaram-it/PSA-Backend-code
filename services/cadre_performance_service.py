import logging
import threading
import time
from typing import Any, TypedDict

from core.config import get_settings
from repositories.cadre_performance_repository import CadrePerformanceRepository
from repositories.cadre_profile_repository import CadreProfileRepository
from utils.cadre_photo import build_cadre_photo_url, build_document_list, filter_valid_mids, normalize_mid

logger = logging.getLogger(__name__)

# Process-level cache for the (expensive, ~20-30s) candidate-pool build. The pool is a
# read-only snapshot of the report_ratings *_nom tables that changes rarely, so we build
# the full list once and serve slices from memory until the TTL lapses. Warmed on startup
# (app.main) so the first navigation is instant, not a 20-30s wait.
_POOL_BUILD_LIMIT = 50000  # matches the /pool route's max limit — build the whole pool once
_POOL_CACHE: dict[str, Any] = {"candidates": None, "ts": 0.0}
_POOL_CACHE_LOCK = threading.Lock()

# Performance-report point fields summed for the "performance" half of Total Score
# (the per-category POINTS columns mapped in CadrePerformanceRepository.map_performance_report).
_PERFORMANCE_POINT_FIELDS = (
    "pedalaSevaloPoints",
    "firstMembershipPoints",
    "renewalPoints",
    "referralPoints",
    "mandalVoteSharePoints",
    "boothVoteSharePoints",
    "mandalMembershipPoints",
    "boothMembershipPoints",
    "mandalD2dPoints",
    "boothD2dPoints",
    "positionsPoints",
)


class ResolvedProfile(TypedDict):
    membershipId: str
    mid: str
    profile: dict[str, Any]
    dakavaraProfile: dict[str, Any] | None
    detailsRow: dict[str, Any] | None
    perfRow: dict[str, Any] | None
    feedback: dict[str, Any] | None
    computedScore: float | None


class CadrePerformanceService:
    def __init__(
        self,
        profile_repo: CadreProfileRepository,
        performance_repo: CadrePerformanceRepository | None = None,
    ):
        self.profile_repo = profile_repo
        self.performance_repo = performance_repo

    @staticmethod
    def is_configured() -> bool:
        return get_settings().report_ratings_configured

    def _require_performance_repo(self) -> CadrePerformanceRepository:
        if not self.performance_repo:
            raise ValueError("report_ratings database is not configured")
        return self.performance_repo

    @staticmethod
    def _compute_total_score(
        performance: dict[str, Any] | None,
        feedback: dict[str, Any] | None,
    ) -> float | None:
        """Total Score = (Σ performance-report points ÷ 2) + (Σ feedback-question points ÷ 2).

        Returns ``None`` only when there is neither performance nor feedback data, so the
        UI can still render an empty/unknown score tier instead of a misleading 0.
        """
        perf_values = (
            [performance.get(field) for field in _PERFORMANCE_POINT_FIELDS]
            if performance else []
        )
        feedback_values = [
            (answer or {}).get("points")
            for answer in (feedback or {}).get("answers", {}).values()
        ]

        has_perf = any(v is not None for v in perf_values)
        has_feedback = any(v is not None for v in feedback_values)
        if not has_perf and not has_feedback:
            return None

        perf_sum = sum(v for v in perf_values if v is not None)
        feedback_sum = sum(v for v in feedback_values if v is not None)
        return perf_sum / 2 + feedback_sum / 2

    def search_candidates(self, mid: str | None = None, mobile: str | None = None, limit: int = 10):
        """
        Search by MID or mobile; return compare-style merged ``profile`` per hit.

        Lookup-first: read ``cadre_details`` when present, else run performance sync procedures.
        """
        if mid and mobile:
            raise ValueError("Provide either MID or mobile, not both")
        if not mid and not mobile:
            raise ValueError("Either MID or mobile is required")

        if mid:
            mid_clean = normalize_mid(mid)
            if not mid_clean:
                raise ValueError("Invalid membership ID")
            dakavara_row = self.profile_repo.get_profile_by_mid(mid_clean)
            resolved = self._resolve_profiles_lookup_first([(mid_clean, dakavara_row)])
            if not resolved:
                raise ValueError(f"No candidate found for membership ID: {mid_clean}")
            items = [_to_search_item(item) for item in resolved]
        else:
            rows = self.profile_repo.search_profiles(mobile=mobile, limit=limit)
            entries: list[tuple[str, dict | None]] = []
            for row in rows:
                membership_id = normalize_mid(row.get("membershipId"))
                if membership_id:
                    entries.append((membership_id, row))
            items = [_to_search_item(item) for item in self._resolve_profiles_lookup_first(entries)]

        # Surface the feedback-question labels alongside each hit so the caller has the
        # full data behind the calculated performanceScore (the answers/points are on item.feedback).
        if items and self.performance_repo:
            questions = self.performance_repo.get_feedback_questions()
            for item in items:
                item["feedbackQuestions"] = questions

        return items

    def list_candidate_pool(self, limit: int = 500) -> dict:
        """Read-only candidate pool from report_ratings.cadre_details, ranked by the
        existing calculated PERFORMANCE SCORE. Served from a process-level cache (built
        once, ~20-30s; rebuilt after cache_ttl_seconds) so navigation is instant — the
        heavy build lives in _build_candidate_pool and is warmed on startup."""
        if not self.performance_repo:
            return {"candidates": [], "total": 0}

        candidates = self._get_pool_candidates_cached()
        sliced = candidates[:limit] if 0 < limit < len(candidates) else candidates
        return {"candidates": sliced, "total": len(candidates)}

    def _get_pool_candidates_cached(self) -> list[dict[str, Any]]:
        """Return the full pool from the process cache, rebuilding on a cold/stale entry.
        A lock + double-check ensures concurrent callers wait on one build, not many."""
        ttl = get_settings().cache_ttl_seconds
        cached = _POOL_CACHE.get("candidates")
        if cached is not None and (time.time() - _POOL_CACHE.get("ts", 0.0)) < ttl:
            return cached
        with _POOL_CACHE_LOCK:
            cached = _POOL_CACHE.get("candidates")
            if cached is not None and (time.time() - _POOL_CACHE.get("ts", 0.0)) < ttl:
                return cached
            built = self._build_candidate_pool(_POOL_BUILD_LIMIT)
            _POOL_CACHE["candidates"] = built
            _POOL_CACHE["ts"] = time.time()
            return built

    def _build_candidate_pool(self, limit: int) -> list[dict[str, Any]]:
        """Heavy pool build (report_ratings snapshot + point breakdown + leader feedback,
        ~20-30s). Called through the cache in list_candidate_pool — do not call directly
        on the request path. Each row carries profile basics plus a grouped point breakdown."""
        repo = self.performance_repo
        rows = repo.list_cadre_details_with_score(limit)
        if not rows:
            return []

        mids = [m for m in (normalize_mid(r.get("membership_id")) for r in rows) if m]
        perf_by_mid: dict[str, dict] = {}
        for perf_row in repo.get_performance_reports_by_mids_nom(mids):
            mapped = repo.map_performance_report(perf_row)
            if mapped and mapped.get("membershipId"):
                perf_by_mid[normalize_mid(mapped["membershipId"])] = mapped

        # Leader feedback, keyed by canonical numeric MID (leader_feedback.membership_id
        # is an INT). Needed so the pool score matches the Nominated Post / Committee
        # profile-detail performance score, which is computed from points + feedback.
        feedback_by_mid: dict[str, dict] = {}
        for fb_row in repo.get_leader_feedback_by_mids(mids):
            mapped_fb = repo.map_leader_feedback(fb_row)
            if mapped_fb and mapped_fb.get("membershipId"):
                feedback_by_mid[_numeric_mid_key(mapped_fb["membershipId"])] = mapped_fb

        image_base = get_settings().cadre_image_base_url
        documents_base = get_settings().nominated_post_documents_base_url
        candidates: list[dict[str, Any]] = []
        for row in rows:
            mid = normalize_mid(row.get("membership_id"))
            if not mid:
                continue
            perf = perf_by_mid.get(mid)
            feedback = feedback_by_mid.get(_numeric_mid_key(mid))
            # Same calculation as the profile detail: (Σ perf points ÷ 2) + (Σ feedback ÷ 2).
            score = self._compute_total_score(perf, feedback)
            candidates.append({
                "id": mid,
                "mid": mid,
                "cadreId": _to_int(row.get("cadre_id")),
                "name": (row.get("name") or "").strip() or f"Cadre {mid}",
                "age": _to_int(row.get("age")),
                "gender": row.get("gender") or "",
                "mobile": row.get("mobile"),
                "parliament": row.get("parliament") or "",
                "assembly": row.get("assembly") or "",
                "mandal": row.get("mandal") or "",
                "village": row.get("village") or "",
                "caste": row.get("caste") or "",
                "category": row.get("category") or "",
                "photoUrl": build_cadre_photo_url(row.get("image"), image_base),
                "score": _round_score(score),
                "perf": _group_performance_points(perf),
                "perfDetail": _detail_performance_points(perf),
                # Previously-applied application (from cadre_details_nom) — the board /
                # department / position the candidate was shortlisted for, and status.
                "board": (row.get("board_name") or "").strip() or None,
                "department": (row.get("department_name") or "").strip() or None,
                "position": (row.get("position_name") or "").strip() or None,
                "appStatus": (row.get("app_status") or "").strip() or None,
                # Year the application was recorded, from npa_updated_time
                # (e.g. '2018-10-03 13:11:08' → '2018'). Shown with Applied position.
                "appliedYear": _year_of(row.get("npa_updated_time")),
                # Supporting documents ($-joined column) → [{name, url}], same shape the
                # Nominated Post / Committee compare Documents overlay consumes.
                "documents": build_document_list(row.get("documents"), documents_base),
            })

        return candidates

    def compare_pool_candidates(self, mids: list[str]):
        """Candidate-pool comparison with the SAME payload shape as
        compare_proposal_candidates, but read entirely from the nominated-post pool
        snapshot + feedback tables — no performance procedures are run. Sources:
        cadre_details_nom (profile + documents + roles), cadre_performace_report_nom
        (points), leader_feedback (per-candidate answers/points/score). Question labels
        come from members_track.question, exactly like the existing compare."""
        cleaned = filter_valid_mids(mids)
        if not cleaned:
            raise ValueError("At least one valid numeric MID is required for compare")
        repo = self._require_performance_repo()

        details_by_mid: dict[str, dict] = {}
        for row in repo.get_cadre_details_nom_by_mids(cleaned):
            mid_key = normalize_mid(_pick_mid_from_details(row))
            if mid_key:
                details_by_mid[mid_key] = row

        perf_by_mid: dict[str, dict] = {}
        for row in repo.get_performance_reports_by_mids_nom(cleaned):
            mid_key = normalize_mid(_pick_mid_from_performance(row))
            if mid_key:
                perf_by_mid[mid_key] = row

        feedback_by_mid: dict[str, dict] = {}
        for row in repo.get_leader_feedback_by_mids(cleaned):
            mapped = repo.map_leader_feedback(row)
            if mapped and mapped.get("membershipId"):
                feedback_by_mid[_numeric_mid_key(mapped["membershipId"])] = mapped

        results = []
        for mid in cleaned:
            details_row = details_by_mid.get(mid)
            perf_row = perf_by_mid.get(mid)
            feedback = feedback_by_mid.get(_numeric_mid_key(mid))
            profile = repo.map_cadre_details_to_profile(details_row)
            cadre_details = repo.serialize_cadre_details(details_row)
            performance = repo.map_performance_report(perf_row)
            computed = self._compute_total_score(performance, feedback)
            if performance is not None and computed is not None:
                performance["performanceScore"] = _round_score(computed)
            photo_url = (profile or {}).get("photoUrl") or (cadre_details or {}).get("photoUrl")
            results.append({
                "membershipId": mid,
                "mid": f"#{mid}",
                "tdpCadreId": (profile or {}).get("tdpCadreId"),
                "photoUrl": photo_url,
                "cadreDetails": cadre_details,
                "cadrePerformanceReport": performance,
                "profile": profile,
                "performance": performance,
                "feedback": feedback,
            })

        return {
            "mids": cleaned,
            "candidates": results,
            "feedbackQuestions": repo.get_feedback_questions(),
        }

    def get_caste_options(self, state_id: int = 1):
        return self.profile_repo.get_caste_options(state_id)

    def update_candidate_caste(self, mid: str, caste_state_id: int):
        """Persist the new caste_state_id on tdp_cadre, re-run the existing performance
        procedures so report_ratings reflects the change, then return the refreshed profile."""
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            raise ValueError("MID is required")
        updated = self.profile_repo.update_caste_state(mid_clean, caste_state_id)
        if not updated:
            raise ValueError(f"No active cadre found for membership ID: {mid_clean}")

        if self.performance_repo:
            self.sync_performance_update([mid_clean])
            self.sync_performance_report([mid_clean])
            resolved = self._resolve_profiles_lookup_first(
                [(mid_clean, None)], sync_if_missing=False
            )
        else:
            dakavara_row = self.profile_repo.get_profile_by_mid(mid_clean)
            resolved = self._resolve_profiles_lookup_first([(mid_clean, dakavara_row)])

        item = resolved[0] if resolved else None
        return {
            "membershipId": mid_clean,
            "mid": f"#{mid_clean}",
            "profile": item["profile"] if item else self.profile_repo.get_profile_by_mid(mid_clean),
            "casteStateId": caste_state_id,
        }

    def get_occupation_options(self):
        return self.profile_repo.get_occupation_options()

    def update_candidate_occupation(self, mid: str, occupation_id: int):
        """Persist the new occupation_id on tdp_cadre, re-run the existing performance
        procedures so report_ratings reflects the change, then return the refreshed profile."""
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            raise ValueError("MID is required")
        updated = self.profile_repo.update_occupation(mid_clean, occupation_id)
        if not updated:
            raise ValueError(f"No active cadre found for membership ID: {mid_clean}")

        if self.performance_repo:
            self.sync_performance_update([mid_clean])
            self.sync_performance_report([mid_clean])
            resolved = self._resolve_profiles_lookup_first(
                [(mid_clean, None)], sync_if_missing=False
            )
        else:
            dakavara_row = self.profile_repo.get_profile_by_mid(mid_clean)
            resolved = self._resolve_profiles_lookup_first([(mid_clean, dakavara_row)])

        item = resolved[0] if resolved else None
        return {
            "membershipId": mid_clean,
            "mid": f"#{mid_clean}",
            "profile": item["profile"] if item else self.profile_repo.get_profile_by_mid(mid_clean),
            "occupationId": occupation_id,
        }

    def get_education_options(self):
        return self.profile_repo.get_education_options()

    def get_party_options(self):
        return self.profile_repo.get_party_options()

    def update_candidate_education(self, mid: str, education_id: int):
        """Persist the new education_id on tdp_cadre, re-run the existing performance
        procedures so report_ratings reflects the change, then return the refreshed profile."""
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            raise ValueError("MID is required")
        updated = self.profile_repo.update_education(mid_clean, education_id)
        if not updated:
            raise ValueError(f"No active cadre found for membership ID: {mid_clean}")

        if self.performance_repo:
            self.sync_performance_update([mid_clean])
            self.sync_performance_report([mid_clean])
            resolved = self._resolve_profiles_lookup_first(
                [(mid_clean, None)], sync_if_missing=False
            )
        else:
            dakavara_row = self.profile_repo.get_profile_by_mid(mid_clean)
            resolved = self._resolve_profiles_lookup_first([(mid_clean, dakavara_row)])

        item = resolved[0] if resolved else None
        return {
            "membershipId": mid_clean,
            "mid": f"#{mid_clean}",
            "profile": item["profile"] if item else self.profile_repo.get_profile_by_mid(mid_clean),
            "educationId": education_id,
        }

    def get_dakavara_profile(self, mid: str | None = None, mobile: str | None = None):
        if mid:
            return self.profile_repo.get_profile_by_mid(mid)
        if mobile:
            return self.profile_repo.get_profile_by_mobile(mobile)
        raise ValueError("Either MID or mobile is required")

    def sync_performance_update(self, mids: list[str]):
        repo = self._require_performance_repo()
        repo.cadre_performance_update(mids)
        logger.info("cadre_performance_update completed mids=%s", len(mids))

    def sync_performance_report(self, mids: list[str]):
        repo = self._require_performance_repo()
        repo.cadre_performance_report(mids)
        logger.info("cadre_performance_report completed mids=%s", len(mids))

    def get_profile_report_by_mid(self, mid: str, *, refresh: bool = False):
        mid_clean = normalize_mid(mid)
        if not mid_clean:
            raise ValueError("MID is required")

        dakavara_row = self.profile_repo.get_profile_by_mid(mid_clean)
        if refresh and self.performance_repo:
            self.sync_performance_update([mid_clean])
            self.sync_performance_report([mid_clean])
            resolved = self._resolve_profiles_lookup_first(
                [(mid_clean, dakavara_row)],
                sync_if_missing=False,
            )
        else:
            resolved = self._resolve_profiles_lookup_first([(mid_clean, dakavara_row)])

        if not resolved:
            raise ValueError(f"Candidate not found for MID: {mid_clean}")

        item = resolved[0]
        performance_repo = self.performance_repo
        performance = (
            performance_repo.map_performance_report(item["perfRow"])
            if performance_repo and item.get("perfRow")
            else None
        )
        if performance is not None and item.get("computedScore") is not None:
            performance["performanceScore"] = item["computedScore"]
        return {
            "membershipId": item["membershipId"],
            "mid": item["mid"],
            "profile": item["profile"],
            "performance": performance,
            "feedback": item.get("feedback"),
        }

    def _sync_all_mids(self, mids: list[str]) -> None:
        """Always run both procedures (update → report) for the requested MIDs so the
        data read afterwards from cadre_details / cadre_performace_report is fresh."""
        self._require_performance_repo()
        cleaned = [m for m in (normalize_mid(mid) for mid in mids) if m]
        if not cleaned:
            return
        logger.info("cadre profile: running performance procedures for %s MID(s)", len(cleaned))
        self.sync_performance_update(cleaned)
        self.sync_performance_report(cleaned)

    def _load_performance_maps(self, mids: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
        performance_repo = self._require_performance_repo()
        cleaned = [m for m in (normalize_mid(mid) for mid in mids) if m]
        details_by_mid: dict[str, dict] = {}
        perf_by_mid: dict[str, dict] = {}
        if not cleaned:
            return details_by_mid, perf_by_mid

        for row in performance_repo.get_cadre_details_by_mids(cleaned):
            mid_key = normalize_mid(_pick_mid_from_details(row))
            if mid_key:
                details_by_mid[mid_key] = row

        for row in performance_repo.get_performance_reports_by_mids(cleaned):
            mid_key = normalize_mid(_pick_mid_from_performance(row))
            if mid_key:
                perf_by_mid[mid_key] = row
        return details_by_mid, perf_by_mid

    def _resolve_profiles_lookup_first(
        self,
        entries: list[tuple[str, dict | None]],
        *,
        sync_if_missing: bool = True,
    ) -> list[ResolvedProfile]:
        """Run both performance procedures for the requested MIDs, then read the
        refreshed cadre_details / cadre_performace_report rows from the DB.

        ``sync_if_missing`` controls whether the procedures run here; callers that
        already ran them (e.g. an explicit refresh) pass ``False`` to avoid a
        double run.
        """
        mids = [membership_id for membership_id, _ in entries if membership_id]
        details_by_mid: dict[str, dict] = {}
        perf_by_mid: dict[str, dict] = {}
        feedback_by_mid: dict[str, dict] = {}

        if self.performance_repo and mids:
            # Lookup-first: read whatever is already materialized in
            # cadre_details / cadre_performace_report.
            details_by_mid, perf_by_mid = self._load_performance_maps(mids)
            if sync_if_missing:
                # Only run the expensive procedures for MIDs that are still missing
                # either row — already-materialized MIDs return instantly.
                missing = [
                    m for m in mids
                    if m not in details_by_mid or m not in perf_by_mid
                ]
                if missing:
                    self._sync_all_mids(missing)
                    fresh_details, fresh_perf = self._load_performance_maps(missing)
                    details_by_mid.update(fresh_details)
                    perf_by_mid.update(fresh_perf)
            # Leader feedback for the Total Score calculation. leader_feedback.membership_id
            # is an INT, so key by a canonical numeric form to match zero-padded MIDs.
            for row in self.performance_repo.get_leader_feedback_by_mids(mids):
                mapped = self.performance_repo.map_leader_feedback(row)
                if mapped and mapped.get("membershipId"):
                    feedback_by_mid[_numeric_mid_key(mapped["membershipId"])] = mapped

        resolved: list[ResolvedProfile] = []
        for membership_id, dakavara_row in entries:
            dakavara = dakavara_row or self.profile_repo.get_profile_by_mid(membership_id)
            details_row = details_by_mid.get(membership_id)
            perf_row = perf_by_mid.get(membership_id)
            feedback = feedback_by_mid.get(_numeric_mid_key(membership_id))

            if not dakavara and not details_row and not perf_row:
                continue

            profile = self._merge_profile_from_sources(dakavara, details_row, perf_row)
            perf_mapped = (
                self.performance_repo.map_performance_report(perf_row)
                if self.performance_repo and perf_row else None
            )
            computed_score = self._compute_total_score(perf_mapped, feedback)
            if computed_score is not None:
                profile["performanceScore"] = computed_score
            resolved.append({
                "membershipId": membership_id,
                "mid": (dakavara or {}).get("mid") or profile.get("mid") or f"#{membership_id}",
                "profile": profile,
                "dakavaraProfile": dakavara or None,
                "detailsRow": details_row,
                "perfRow": perf_row,
                "feedback": feedback,
                "computedScore": computed_score,
            })
        return resolved

    def _merge_profile_from_sources(
        self,
        dakavara_profile: dict | None,
        details_row: dict | None,
        perf_row: dict | None,
    ) -> dict:
        performance_repo = self.performance_repo
        cadre_details = (
            performance_repo.serialize_cadre_details(details_row) if performance_repo else None
        )
        cadre_performance_report = (
            performance_repo.map_performance_report(perf_row) if performance_repo else None
        )
        report_profile = (
            performance_repo.map_cadre_details_to_profile(details_row) if performance_repo else None
        )
        return _merge_compare_profile(
            dakavara_profile,
            report_profile,
            cadre_performance_report,
            cadre_details,
        )

    def attach_profiles_to_candidates(self, candidates: list[dict]) -> list[dict]:
        """Add compare-style merged ``profile`` to each proposal candidate row."""
        if not candidates:
            return []

        profile_by_mid = self.build_profile_map_for_candidates(candidates)
        enriched: list[dict] = []
        for cand in candidates:
            row = dict(cand)
            mid = _mid_from_candidate_record(row, self.profile_repo)
            if mid and mid in profile_by_mid:
                row["profile"] = profile_by_mid[mid]
            enriched.append(row)
        return enriched

    def build_profile_map_for_candidates(self, candidates: list[dict]) -> dict[str, dict]:
        mids: list[str] = []
        seen: set[str] = set()
        for cand in candidates:
            mid = _mid_from_candidate_record(cand, self.profile_repo)
            if mid and mid not in seen:
                seen.add(mid)
                mids.append(mid)
        if not mids:
            return {}

        resolved = self._resolve_profiles_lookup_first([(mid, None) for mid in mids])
        return {item["membershipId"]: item["profile"] for item in resolved}

    def compare_candidates(self, mids: list[str], proposal_candidates: list[dict] | None = None):
        cleaned = filter_valid_mids(mids)
        if not cleaned:
            raise ValueError("At least one valid numeric MID is required for compare")

        performance_repo = self._require_performance_repo()
        resolved = self._resolve_profiles_lookup_first([(mid, None) for mid in cleaned])
        resolved_by_mid = {item["membershipId"]: item for item in resolved}

        meta_by_mid: dict[str, dict] = {}
        if proposal_candidates:
            for cand in proposal_candidates:
                mid_key = normalize_mid(cand.get("membership_id") or cand.get("membershipId"))
                if mid_key:
                    meta_by_mid[mid_key] = cand

        results = []
        for mid in cleaned:
            item = resolved_by_mid.get(mid)
            if not item:
                raise ValueError(
                    f"No profile or performance data found for MID {mid} after sync. "
                    "Verify the membership ID exists in dakavara_pa."
                )

            meta = meta_by_mid.get(mid, {})
            dakavara_profile = item["dakavaraProfile"] or {}
            details_row = item["detailsRow"]
            perf_row = item["perfRow"]
            profile = item["profile"]
            cadre_details = performance_repo.serialize_cadre_details(details_row)
            cadre_performance_report = performance_repo.map_performance_report(perf_row)
            if cadre_performance_report is not None and item.get("computedScore") is not None:
                cadre_performance_report["performanceScore"] = item["computedScore"]
            photo_url = (
                profile.get("photoUrl")
                or (cadre_details or {}).get("photoUrl")
                or dakavara_profile.get("photoUrl")
            )

            results.append({
                "membershipId": mid,
                "mid": f"#{mid}",
                "tdpCadreId": (
                    profile.get("tdpCadreId")
                    or meta.get("tdp_cadre_id")
                    or meta.get("tdpCadreId")
                ),
                "proposalCandidateId": (
                    meta.get("proposal_candidate_id") or meta.get("proposalCandidateId")
                ),
                "photoUrl": photo_url,
                "dakavaraProfile": dakavara_profile if dakavara_profile else None,
                "cadreDetails": cadre_details,
                "cadrePerformanceReport": cadre_performance_report,
                "profile": profile,
                "performance": cadre_performance_report,
                "feedback": item.get("feedback"),
            })

        return {
            "mids": cleaned,
            "candidates": results,
            "feedbackQuestions": performance_repo.get_feedback_questions(),
        }


def _to_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year_of(value):
    """Extract the 4-digit year from an npa_updated_time value (datetime or string
    like '2018-10-03 13:11:08'). Returns the year as a string, or None."""
    if value is None:
        return None
    year = getattr(value, "year", None)
    if year:
        return str(year)
    text = str(value).strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


def _round_score(value):
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _group_performance_points(perf: dict | None) -> dict:
    """Collapse the 11 performance-report point fields into the 5 categories shown
    in the candidate drawer. A category is None when none of its fields are present."""
    def total(*keys):
        vals = [(perf or {}).get(k) for k in keys]
        present = [v for v in vals if v is not None]
        return round(sum(present), 2) if present else None

    return {
        "pedalaSevalo": total("pedalaSevaloPoints"),
        "membership": total(
            "firstMembershipPoints", "renewalPoints", "referralPoints",
            "mandalMembershipPoints", "boothMembershipPoints",
        ),
        "voteShare": total("mandalVoteSharePoints", "boothVoteSharePoints"),
        "d2d": total("mandalD2dPoints", "boothD2dPoints"),
        "positions": total("positionsPoints"),
    }


def _detail_performance_points(perf: dict | None) -> dict | None:
    """Fine-grained performance-report fields for the candidate drawer's detailed
    points breakdown + field-metrics section. Returns None when the cadre has no
    performance report, so the UI can fall back to the 5-category grouped view.
    All values come straight from the already-mapped report (report_ratings)."""
    if not perf:
        return None

    point_keys = (
        "pedalaSevaloPoints", "firstMembershipPoints", "renewalPoints", "referralPoints",
        "mandalVoteSharePoints", "boothVoteSharePoints", "mandalMembershipPoints",
        "boothMembershipPoints", "mandalD2dPoints", "boothD2dPoints", "positionsPoints",
    )
    field_keys = (
        "mandalD2dAchPercent", "boothD2dAchPercent",
        "mandalVoteSharePercent", "boothVoteSharePercent",
        "renewalTimes", "firstMembershipYear",
    )
    detail = {k: perf.get(k) for k in (*point_keys, *field_keys)}
    present = [perf.get(k) for k in point_keys if perf.get(k) is not None]
    detail["breakdownTotal"] = round(sum(present), 2) if present else None
    return detail


def _numeric_mid_key(mid: str | None) -> str:
    """Canonical key for matching MIDs across varchar (zero-padded) and int sources."""
    cleaned = normalize_mid(mid)
    return str(int(cleaned)) if cleaned.isdigit() else cleaned


def _to_search_item(resolved: ResolvedProfile) -> dict:
    return {
        "membershipId": resolved["membershipId"],
        "mid": resolved["mid"],
        "profile": resolved["profile"],
        "feedback": resolved.get("feedback"),
    }


def _merge_compare_profile(
    dakavara_profile: dict | None,
    report_profile: dict | None,
    performance: dict | None,
    cadre_details: dict | None,
) -> dict:
    merged = dict(dakavara_profile or {})
    if report_profile:
        for key, value in report_profile.items():
            if value is not None:
                merged[key] = value
    if performance:
        if performance.get("renewalTimes") is not None:
            merged["renewalTimes"] = performance["renewalTimes"]
        if performance.get("mandalVoteSharePercent") is not None:
            merged["constituencyPercent"] = performance["mandalVoteSharePercent"]
        if performance.get("performanceScore") is not None:
            merged["performanceScore"] = performance["performanceScore"]
        if performance.get("firstMembershipYear") is not None:
            merged["firstMembershipYear"] = performance["firstMembershipYear"]
    if cadre_details:
        if cadre_details.get("photoUrl"):
            merged["photoUrl"] = cadre_details["photoUrl"]
        if cadre_details.get("IMAGE"):
            merged["photo"] = cadre_details["IMAGE"]
    if not merged.get("photoUrl") and merged.get("photo"):
        merged["photoUrl"] = build_cadre_photo_url(
            merged["photo"], get_settings().cadre_image_base_url
        )
    return merged


def _mid_from_candidate_record(
    cand: dict,
    profile_repo: CadreProfileRepository | None = None,
) -> str:
    mid = normalize_mid(cand.get("membership_id") or cand.get("membershipId"))
    if mid:
        return mid
    if not profile_repo:
        return ""
    tdp_id = cand.get("tdp_cadre_id") or cand.get("tdpCadreId")
    if not tdp_id:
        return ""
    try:
        profile = profile_repo.get_profile_by_cadre_id(int(tdp_id))
    except (TypeError, ValueError):
        return ""
    return normalize_mid((profile or {}).get("membershipId"))


def _pick_mid_from_details(row: dict) -> str:
    return row.get("membership_id") or row.get("membershipId") or ""


def _pick_mid_from_performance(row: dict) -> str:
    return row.get("#MID") or row.get("MID") or row.get("membership_id") or row.get("membershipId") or ""
