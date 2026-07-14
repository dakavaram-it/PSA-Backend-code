import logging
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Must match get_capacity_from_legacy() in nominated_proposal_repository.py
_CONFIRMED_LEGACY_STATUSES = (
    "'confirmed','finalized','published','approved'"
)
_CONFIRMED_SEAT_EXPR = (
    f"SUM(CASE WHEN LOWER(COALESCE(NPS.status,'')) IN ({_CONFIRMED_LEGACY_STATUSES}) "
    "THEN 1 ELSE 0 END)"
)


class NominatedRepository:
    def __init__(self, dakavara_db: Session, pa_track_db: Session):
        self.dakavara_db = dakavara_db
        self.pa_track_db = pa_track_db

    @staticmethod
    def _rows_to_dict(rows):
        """
        Convert SQLAlchemy RowMapping result list into JSON-serializable dict list.
        This avoids:
        PydanticSerializationError: Unable to serialize unknown type: RowMapping
        """
        return [dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row):
        """
        Convert a single SQLAlchemy RowMapping into dict.
        """
        return dict(row) if row else None

    def get_post_types(self):
        sql = text("""
            SELECT post_type_id AS id, post_name AS name
            FROM post_type
            ORDER BY post_type_id
        """)
        rows = self.pa_track_db.execute(sql).mappings().all()
        return self._rows_to_dict(rows)

    def get_levels(self):
        sql = text("""
            SELECT board_level_id AS id, level AS name
            FROM board_level
            ORDER BY board_level_id
        """)
        rows = self.dakavara_db.execute(sql).mappings().all()
        return self._rows_to_dict(rows)

    def get_states(self):
        sql = text("""
            SELECT state_id AS id, state_name AS name
            FROM state
            WHERE state_id IN (1,36)
            ORDER BY state_name
        """)
        rows = self.dakavara_db.execute(sql).mappings().all()
        return self._rows_to_dict(rows)

    def get_locations(self, board_level_id: int, state_id: int | None):
        # Adjust mappings if your existing SpringBoot API uses different values.
        if board_level_id == 3:  # District
            sql = text("""
                SELECT district_id AS id, district_name AS name
                FROM district
                WHERE (:state_id IS NULL OR state_id = :state_id)
                ORDER BY district_name
            """)
            rows = self.dakavara_db.execute(sql, {"state_id": state_id}).mappings().all()
            return self._rows_to_dict(rows)

        if board_level_id in (4,):  # Assembly
            sql = text("""
                SELECT constituency_id AS id, name
                FROM constituency
                WHERE constituency_type_id = 1
                ORDER BY name
            """)
            rows = self.dakavara_db.execute(sql).mappings().all()
            return self._rows_to_dict(rows)

        if board_level_id in (9, 10, 11):  # optional PC custom levels if present in UI
            sql = text("""
                SELECT constituency_id AS id, name
                FROM constituency
                WHERE constituency_type_id = 2
                ORDER BY name
            """)
            rows = self.dakavara_db.execute(sql).mappings().all()
            return self._rows_to_dict(rows)

        if board_level_id == 5:  # Mandal
            sql = text("""
                SELECT tehsil_id AS id, tehsil_name AS name
                FROM tehsil
                ORDER BY tehsil_name
            """)
            rows = self.dakavara_db.execute(sql).mappings().all()
            return self._rows_to_dict(rows)

        if board_level_id == 7:  # Village
            sql = text("""
                SELECT panchayat_id AS id, panchayat_name AS name
                FROM panchayat
                ORDER BY panchayat_name
            """)
            rows = self.dakavara_db.execute(sql).mappings().all()
            return self._rows_to_dict(rows)

        # Central/State: location is state itself.
        return self.get_states()

    def _get_member_seat_rows(
        self,
        enrollment_id: int,
        board_level_id: int,
        location_value: int,
        department_id: int | None = None,
        board_id: int | None = None,
    ):
        """
        Per nominated_post_member slot: department, board, position, capacity, legacy fills.
        """
        dept_filter = "AND D.department_id = :department_id" if department_id is not None else ""
        board_filter = "AND B.board_id = :board_id" if board_id is not None else ""
        sql = text(f"""
            SELECT
                D.department_id AS departmentId,
                D.dept_name AS departmentName,
                B.board_id AS boardId,
                B.board_name AS boardName,
                P.position_id AS positionId,
                P.position_name AS positionName,
                NPM.nominated_post_member_id AS nominatedPostMemberId,
                NPM.nominated_post_position_id AS nominatedPostPositionId,
                NPM.max_members AS maxMembers,
                {_CONFIRMED_SEAT_EXPR} AS confirmedSeats
            FROM nominated_post_member NPM
            JOIN nominated_post_position NPP
                ON NPM.nominated_post_position_id = NPP.nominated_post_position_id
                AND NPP.is_deleted = 'N'
            JOIN departments D ON NPP.department_id = D.department_id
            JOIN board B ON NPP.board_id = B.board_id
            JOIN position P ON NPP.position_id = P.position_id
            LEFT JOIN nominated_post NP
                ON NPM.nominated_post_member_id = NP.nominated_post_member_id
                AND NP.is_deleted = 'N'
            LEFT JOIN nominated_post_status NPS
                ON NP.nominated_post_status_id = NPS.nominated_post_status_id
            WHERE NPM.is_deleted = 'N'
              AND NPM.enrollment_id = :enrollment_id
              AND NPM.board_level_id = :board_level_id
              AND NPM.location_value = :location_value
              {dept_filter}
              {board_filter}
            GROUP BY
                D.department_id,
                D.dept_name,
                B.board_id,
                B.board_name,
                P.position_id,
                P.position_name,
                NPM.nominated_post_member_id,
                NPM.nominated_post_position_id,
                NPM.max_members
        """)
        params = {
            "enrollment_id": enrollment_id,
            "board_level_id": board_level_id,
            "location_value": location_value,
        }
        if department_id is not None:
            params["department_id"] = department_id
        if board_id is not None:
            params["board_id"] = board_id
        rows = self.dakavara_db.execute(sql, params).mappings().all()
        return self._rows_to_dict(rows)

    def _get_active_proposed_counts_by_members(self, member_ids: list[int]) -> dict[int, int]:
        if not member_ids:
            return {}
        sql = text("""
            SELECT
                p.nominated_post_member_id AS nominatedPostMemberId,
                COUNT(c.proposal_candidate_id) AS proposedCount
            FROM nominated_post_proposal p
            JOIN nominated_post_proposal_candidate c
                ON p.proposal_id = c.proposal_id AND c.is_deleted = 'N'
            WHERE p.nominated_post_member_id IN :member_ids
              AND p.is_deleted = 'N'
              AND p.current_status_code NOT IN ('REJECTED', 'PUBLISHED')
            GROUP BY p.nominated_post_member_id
        """).bindparams(bindparam("member_ids", expanding=True))
        rows = self.pa_track_db.execute(sql, {"member_ids": member_ids}).mappings().all()
        return {
            int(row["nominatedPostMemberId"]): int(row["proposedCount"] or 0)
            for row in rows
        }

    @staticmethod
    def _open_seats_for_member(max_members: int, confirmed: int, proposed: int) -> int:
        return max(max_members - confirmed - proposed, 0)

    def _aggregate_seat_groups(
        self,
        seat_rows: list[dict],
        proposed_by_member: dict[int, int],
        group_id_key: str,
        group_name_key: str,
        *,
        only_with_unfilled: bool = True,
    ) -> list[dict]:
        aggregated: dict[int, dict] = {}
        for row in seat_rows:
            max_members = int(row.get("maxMembers") or 0)
            confirmed = int(row.get("confirmedSeats") or 0)
            member_id = int(row["nominatedPostMemberId"])
            proposed = proposed_by_member.get(member_id, 0)
            unfilled = self._open_seats_for_member(max_members, confirmed, proposed)

            filled = max_members - unfilled

            group_id = int(row[group_id_key])
            if group_id not in aggregated:
                aggregated[group_id] = {
                    "id": group_id,
                    "name": row[group_name_key],
                    "totalSeats": 0,
                    "filledSeats": 0,
                    "unfilledSeats": 0,
                }
            aggregated[group_id]["totalSeats"] += max_members
            aggregated[group_id]["filledSeats"] += filled
            aggregated[group_id]["unfilledSeats"] += unfilled

        results = list(aggregated.values())
        if only_with_unfilled:
            results = [item for item in results if item["unfilledSeats"] > 0]
        return sorted(results, key=lambda item: item["name"])

    def _seat_summary_for_members(
        self,
        seat_rows: list[dict],
        proposed_by_member: dict[int, int],
        group_id_key: str,
        group_name_key: str,
        *,
        only_with_unfilled: bool = True,
    ) -> list[dict]:
        if not seat_rows:
            return []
        return self._aggregate_seat_groups(
            seat_rows,
            proposed_by_member,
            group_id_key,
            group_name_key,
            only_with_unfilled=only_with_unfilled,
        )

    def get_departments(self, enrollment_id: int, board_level_id: int, location_value: int):
        """
        All departments for the level/location with totalSeats, filledSeats, and
        unfilledSeats aggregated per department.
        """
        seat_rows = self._get_member_seat_rows(
            enrollment_id, board_level_id, location_value
        )
        if not seat_rows:
            return []

        member_ids = [int(row["nominatedPostMemberId"]) for row in seat_rows]
        proposed_by_member = self._get_active_proposed_counts_by_members(member_ids)
        return self._seat_summary_for_members(
            seat_rows,
            proposed_by_member,
            "departmentId",
            "departmentName",
            only_with_unfilled=False,
        )

    def get_boards(self, enrollment_id: int, board_level_id: int, location_value: int, department_id: int | None = None):
        """
        Boards with totalSeats, filledSeats, and unfilledSeats. When department_id
        is given, scopes to that department. When omitted, returns ALL boards for
        the level/location, each carrying its owning departmentId/departmentName so
        a board can be picked directly (the department is needed for Create Position).
        """
        seat_rows = self._get_member_seat_rows(
            enrollment_id, board_level_id, location_value, department_id
        )
        if not seat_rows:
            return []

        member_ids = [int(row["nominatedPostMemberId"]) for row in seat_rows]
        proposed_by_member = self._get_active_proposed_counts_by_members(member_ids)
        summaries = self._seat_summary_for_members(
            seat_rows,
            proposed_by_member,
            "boardId",
            "boardName",
            only_with_unfilled=False,
        )
        if department_id is None:
            summaries = self._attach_department_for_boards(seat_rows, summaries)
        return summaries

    def _attach_department_for_boards(
        self,
        seat_rows: list[dict],
        board_summaries: list[dict],
    ) -> list[dict]:
        """Attach the owning departmentId/departmentName to each board summary so a
        directly-picked board carries the department needed to create a position."""
        dept_by_board: dict[int, dict] = {}
        for row in seat_rows:
            board_id = int(row["boardId"])
            if board_id not in dept_by_board:
                dept_by_board[board_id] = {
                    "departmentId": int(row["departmentId"]),
                    "departmentName": row["departmentName"],
                }
        for item in board_summaries:
            dept = dept_by_board.get(item["id"])
            if dept:
                item["departmentId"] = dept["departmentId"]
                item["departmentName"] = dept["departmentName"]
        return board_summaries

    def _attach_member_ids_for_positions(
        self,
        seat_rows: list[dict],
        proposed_by_member: dict[int, int],
        position_summaries: list[dict],
    ) -> list[dict]:
        """Prefer the member slot with the most unfilled seats for proposal wiring."""
        slots_by_position: dict[int, dict] = {}
        for row in seat_rows:
            position_id = int(row["positionId"])
            max_members = int(row.get("maxMembers") or 0)
            confirmed = int(row.get("confirmedSeats") or 0)
            member_id = int(row["nominatedPostMemberId"])
            proposed = proposed_by_member.get(member_id, 0)
            unfilled = self._open_seats_for_member(max_members, confirmed, proposed)
            slot = {
                "unfilled": unfilled,
                "nominatedPostMemberId": member_id,
                "nominatedPostPositionId": int(row["nominatedPostPositionId"]),
            }
            existing = slots_by_position.get(position_id)
            if existing is None or slot["unfilled"] > existing["unfilled"]:
                slots_by_position[position_id] = slot

        for item in position_summaries:
            slot = slots_by_position.get(item["id"])
            if slot:
                item["nominatedPostMemberId"] = slot["nominatedPostMemberId"]
                item["nominatedPostPositionId"] = slot["nominatedPostPositionId"]
        return position_summaries

    def get_positions(self, enrollment_id: int, board_level_id: int, location_value: int, department_id: int, board_id: int):
        """
        Positions for selected department + board with totalSeats, filledSeats,
        unfilledSeats, plus nominatedPostMemberId / nominatedPostPositionId for proposals.
        """
        seat_rows = self._get_member_seat_rows(
            enrollment_id,
            board_level_id,
            location_value,
            department_id=department_id,
            board_id=board_id,
        )
        if not seat_rows:
            return []

        member_ids = [int(row["nominatedPostMemberId"]) for row in seat_rows]
        proposed_by_member = self._get_active_proposed_counts_by_members(member_ids)
        summaries = self._seat_summary_for_members(
            seat_rows,
            proposed_by_member,
            "positionId",
            "positionName",
            only_with_unfilled=False,
        )
        return self._attach_member_ids_for_positions(
            seat_rows, proposed_by_member, summaries
        )

    def get_position_capacity(
        self,
        enrollment_id: int,
        board_level_id: int,
        location_value: int,
        department_id: int,
        board_id: int,
        position_id: int
    ):
        sql = text(f"""
            SELECT
                P.position_id AS positionId,
                P.position_name AS positionName,
                MAX(NPM.max_members) AS totalSeats,
                {_CONFIRMED_SEAT_EXPR} AS confirmedSeats,
                GREATEST(MAX(NPM.max_members) - {_CONFIRMED_SEAT_EXPR}, 0) AS openSeats,
                NPM.nominated_post_member_id AS nominatedPostMemberId,
                NPM.nominated_post_position_id AS nominatedPostPositionId
            FROM nominated_post_member NPM
            JOIN nominated_post_position NPP ON NPM.nominated_post_position_id = NPP.nominated_post_position_id AND NPP.is_deleted = 'N'
            JOIN departments D ON NPP.department_id = D.department_id
            JOIN board B ON NPP.board_id = B.board_id
            JOIN position P ON NPP.position_id = P.position_id
            LEFT JOIN nominated_post NP ON NPM.nominated_post_member_id = NP.nominated_post_member_id AND NP.is_deleted = 'N'
            LEFT JOIN nominated_post_status NPS ON NP.nominated_post_status_id = NPS.nominated_post_status_id
            WHERE NPM.is_deleted = 'N'
              AND NPM.enrollment_id = :enrollment_id
              AND NPM.board_level_id = :board_level_id
              AND NPM.location_value = :location_value
              AND D.department_id = :department_id
              AND B.board_id = :board_id
              AND P.position_id = :position_id
            GROUP BY P.position_id, P.position_name, NPM.nominated_post_member_id, NPM.nominated_post_position_id
            ORDER BY openSeats DESC
            LIMIT 1
        """)
        row = self.dakavara_db.execute(sql, {
            "enrollment_id": enrollment_id,
            "board_level_id": board_level_id,
            "location_value": location_value,
            "department_id": department_id,
            "board_id": board_id,
            "position_id": position_id,
        }).mappings().first()
        return self._row_to_dict(row)

    def search_cadre(self, query: str, limit: int = 10):
        like = f"%{query}%"
        sql = text("""
            SELECT
                CR.tdp_cadre_id AS cadreId,
                CONCAT('#', CR.membership_id) AS mid,
                CR.first_name AS firstName,
                CR.last_name AS lastName,
                TRIM(CONCAT(COALESCE(CR.first_name,''),' ',COALESCE(CR.last_name,''))) AS memberName,
                CR.mobile_no AS mobileNo,
                CR.gender AS gender
            FROM tdp_cadre CR
            WHERE CR.is_deleted = 'N'
              AND (
                    CR.mobile_no = :query
                    OR CR.membership_id = :query
                    OR CONCAT('#', CR.membership_id) = :query
                    OR CR.mobile_no LIKE :like
                    OR CR.membership_id LIKE :like
                  )
            ORDER BY CR.tdp_cadre_id DESC
            LIMIT :limit
        """)
        rows = self.dakavara_db.execute(sql, {
            "query": query,
            "like": like,
            "limit": limit
        }).mappings().all()
        return self._rows_to_dict(rows)
