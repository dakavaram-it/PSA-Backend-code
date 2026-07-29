import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.utils.cadre_photo import normalize_mid

logger = logging.getLogger(__name__)

# MY TDP APP USAGE — points/ranks + feed (posts/events) counts for one membership.
# Runs against the mytdp DB (sir_session). The constituency_rank subquery is heavy
# (full user_points scan bounded to one assembly), so this is fetched lazily via a
# dedicated endpoint rather than inside the main profile-by-MID call.
_APP_USAGE_SQL = text(
    """
    SELECT
        X.membership_id, X.user_id, X.total_points,
        (SELECT COUNT(*) + 1
         FROM user_points UP2
         WHERE UP2.total_points > X.total_points) AS state_rank,
        (SELECT COUNT(*) + 1
         FROM user_points UP3
         JOIN user U3            ON UP3.user_id = U3.id
         JOIN person_address PA3 ON U3.person_id = PA3.person_id
         JOIN address A3         ON PA3.address_id = A3.id
         JOIN booth B3           ON A3.booth_id = B3.id
         WHERE B3.assembly_id = X.constituency_id
           AND UP3.total_points > X.total_points) AS constituency_rank,
        (SELECT COUNT(*)
         FROM feed_posts P
         JOIN user_memberships UMx ON P.user_id = UMx.user_id
         WHERE UMx.membership_id = X.membership_id
           AND P.created_at >= '2026-01-01'
           AND P.created_at <  '2027-01-01') AS total_count,
        (SELECT SUM(P.category = 'POST')
         FROM feed_posts P
         JOIN user_memberships UMx ON P.user_id = UMx.user_id
         WHERE UMx.membership_id = X.membership_id
           AND P.created_at >= '2024-01-01'
           AND P.created_at <  '2027-01-01') AS post_count,
        (SELECT SUM(P.category = 'EVENT')
         FROM feed_posts P
         JOIN user_memberships UMx ON P.user_id = UMx.user_id
         WHERE UMx.membership_id = X.membership_id
           AND P.created_at >= '2024-01-01'
           AND P.created_at <  '2027-01-01') AS event_count
    FROM (
        SELECT
            UM.membership_id,
            UP.user_id,
            UP.total_points,
            B.assembly_id AS constituency_id
        FROM user_points UP
        JOIN user_memberships UM ON UP.user_id = UM.user_id
        JOIN user U             ON UP.user_id = U.id
        JOIN person_address PA  ON U.person_id = PA.person_id
        JOIN address A          ON PA.address_id = A.id
        JOIN booth B            ON A.booth_id = B.id
        WHERE UM.membership_id = :mid
        LIMIT 1
    ) X
    """
)

# Fallback used only when the full query fails because feed_posts is unreachable
# (e.g. read-only accounts that aren't granted the feed table). Points + ranks
# still come back; feed counts are returned as null.
_APP_USAGE_RANKS_ONLY_SQL = text(
    """
    SELECT
        X.membership_id, X.user_id, X.total_points,
        (SELECT COUNT(*) + 1
         FROM user_points UP2
         WHERE UP2.total_points > X.total_points) AS state_rank,
        (SELECT COUNT(*) + 1
         FROM user_points UP3
         JOIN user U3            ON UP3.user_id = U3.id
         JOIN person_address PA3 ON U3.person_id = PA3.person_id
         JOIN address A3         ON PA3.address_id = A3.id
         JOIN booth B3           ON A3.booth_id = B3.id
         WHERE B3.assembly_id = X.constituency_id
           AND UP3.total_points > X.total_points) AS constituency_rank
    FROM (
        SELECT
            UM.membership_id,
            UP.user_id,
            UP.total_points,
            B.assembly_id AS constituency_id
        FROM user_points UP
        JOIN user_memberships UM ON UP.user_id = UM.user_id
        JOIN user U             ON UP.user_id = U.id
        JOIN person_address PA  ON U.person_id = PA.person_id
        JOIN address A          ON PA.address_id = A.id
        JOIN booth B            ON A.booth_id = B.id
        WHERE UM.membership_id = :mid
        LIMIT 1
    ) X
    """
)


class CadreAppUsageRepository:
    """MY TDP APP USAGE reads from the mytdp app DB (projectk cluster)."""

    def __init__(self, db: Session | None):
        self.db = db

    @staticmethod
    def _candidate_mids(mid: str) -> list[str]:
        """Membership ids in mytdp are zero-padded to 8 digits (e.g. '03842349').
        Try the cleaned id first, then a zero-padded variant so shorter ids match."""
        cleaned = normalize_mid(mid)
        if not cleaned:
            return []
        candidates = [cleaned]
        if cleaned.isdigit() and len(cleaned) < 8:
            padded = cleaned.zfill(8)
            if padded != cleaned:
                candidates.append(padded)
        return candidates

    @staticmethod
    def _shape(row) -> dict:
        data = dict(row)

        def _num(value):
            if value is None:
                return None
            return float(value) if isinstance(value, float) or "." in str(value) else int(value)

        return {
            "membershipId": data.get("membership_id"),
            "userId": data.get("user_id"),
            "totalPoints": _num(data.get("total_points")),
            "stateRank": _num(data.get("state_rank")),
            "constituencyRank": _num(data.get("constituency_rank")),
            "totalCount": _num(data.get("total_count")),
            "postCount": _num(data.get("post_count")),
            "eventCount": _num(data.get("event_count")),
        }

    def _empty(self, mid: str) -> dict:
        return {
            "membershipId": normalize_mid(mid),
            "userId": None,
            "totalPoints": None,
            "stateRank": None,
            "constituencyRank": None,
            "totalCount": None,
            "postCount": None,
            "eventCount": None,
        }

    def get_app_usage_by_mid(self, mid: str) -> dict:
        if self.db is None:
            logger.warning("mytdp database is not configured; returning empty app usage")
            return self._empty(mid)
        for candidate in self._candidate_mids(mid):
            try:
                row = self.db.execute(_APP_USAGE_SQL, {"mid": candidate}).mappings().first()
            except Exception as exc:
                # feed_posts may be unreachable in some environments — fall back to
                # points/ranks only rather than failing the whole request.
                logger.warning("app_usage full query failed (feed_posts?) mid=%s error=%s", candidate, exc)
                self.db.rollback()
                row = self.db.execute(
                    _APP_USAGE_RANKS_ONLY_SQL, {"mid": candidate}
                ).mappings().first()
            if row:
                return self._shape(row)
        return self._empty(mid)
