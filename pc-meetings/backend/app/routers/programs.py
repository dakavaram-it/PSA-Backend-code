"""Program meetings, and the Programmes page's role/activity/leader tracking.

The `/{program_id}/...` routes below are a different, still-unwired feature
(individual programme *events*, mirroring the `meetings` domain) — their
contract survives the removal of upstream wiring, the bodies do not.

The role/activity-summary/leaders routes are real and live off four
`party_track` tables: `role` (already populated, shared with `/roles`),
`leader` (the 71k-row real roster), `program` and `program_role` (the
catalog of trackable programmes and which roles they apply to), and
`leader_program_activity` (one row per leader/programme/month once someone
records participation). The last three are freshly added and still empty in
production, so Updated/Not updated and the whole activity-summary card read
zero/empty until the party starts populating them — Total/Members do not,
since those come straight off the `leader` roster.

`party_track.activity` is a separate, long-lived scoring system
(`leader_activity`, ~800k rows, weighted points) unrelated to this feature —
`/activities` below still serves it for whatever already depends on it, but
the Programmes page's own "Activity" column and filter are `program`, since
`leader_program_activity.program_id` is what it actually joins against.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import config, db

router = APIRouter(prefix="/api/programs", tags=["programs"])

_UNWIRED = "No data source configured"


def _month_id(year: int, month: int) -> int | None:
    """`party_track.month` is a short, hand-seeded lookup table — a period
    outside it (the current calendar month usually is, until someone extends
    it) has no leader_program_activity rows to find either way, so `None`
    here just means the WHERE below matches nothing, not an error."""
    return db.scalar(
        f"SELECT month_id FROM {config.PARTY_TRACK_DB}.month WHERE year = %s AND month_no = %s",
        (year, month),
    )


@router.get("")
async def list_programs(
    from_date: str = Query(..., alias="from", description="YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="YYYY-MM-DD"),
) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail=_UNWIRED)


@router.get("/roles")
def list_roles() -> list[dict[str, Any]]:
    """Every member designation a programme can be filtered by (Minister, MLA,
    MP, Mandal President, …) — a fixed roster, not a per-programme or
    per-period breakdown.

    This is `party_track.role`, not `mytdp.role`: the latter is committee-
    meeting org tiers (State/District/.../Booth), a different taxonomy that
    answers a different question. Unlike the routes above, `party_track.role`
    is real and already populated, so this one is wired for real rather than
    raising 501.
    """
    rows = db.rows(
        f"""SELECT role_id, role_name FROM {config.PARTY_TRACK_DB}.role
             WHERE is_active IS NULL OR is_active = 'Y'
             ORDER BY (order_no IS NULL), order_no, role_name"""
    )
    return [{"id": r["role_id"], "name": r["role_name"]} for r in rows]


@router.get("/activities")
def list_activities() -> list[dict[str, Any]]:
    """Every activity type a programme can be filtered by (Membership Drive,
    Pressmeets, Door 2 Door Campaign, …) — `party_track.activity`, the same
    sibling schema `/roles` reads, and just as real and already populated."""
    rows = db.rows(
        f"SELECT activity_id, activity_name FROM {config.PARTY_TRACK_DB}.activity ORDER BY activity_id"
    )
    return [{"id": r["activity_id"], "name": r["activity_name"]} for r in rows]


@router.get("/role-summary")
def role_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
) -> list[dict[str, Any]]:
    """The first Programmes card: every active role, its `leader` roster
    size, and how many of those leaders logged at least one
    `leader_program_activity` row (against any programme) in this month.

    `total` counts every leader row for the role, `members` only the ones
    not soft-deleted — the two differ exactly where leaders have left since
    being added, the same way `ACI`'s 50 rows split into 42 active today.
    """
    month_id = _month_id(year, month)
    rows = db.rows(
        f"""SELECT r.role_id, r.role_name,
                   COUNT(l.leader_id) AS total,
                   SUM(l.is_deleted = 'N') AS members,
                   SUM(l.is_deleted = 'N' AND u.leader_id IS NOT NULL) AS updated
              FROM {config.PARTY_TRACK_DB}.role r
              LEFT JOIN {config.PARTY_TRACK_DB}.leader l ON l.role_id = r.role_id
              LEFT JOIN (
                    SELECT DISTINCT leader_id FROM {config.PARTY_TRACK_DB}.leader_program_activity
                     WHERE month_id = %s AND (is_deleted IS NULL OR is_deleted = 'N')
                   ) u ON u.leader_id = l.leader_id
             WHERE r.is_active IS NULL OR r.is_active = 'Y'
             GROUP BY r.role_id, r.role_name
             ORDER BY (r.order_no IS NULL), r.order_no, r.role_name""",
        (month_id,),
    )
    out = []
    for r in rows:
        members = int(r["members"] or 0)
        updated = int(r["updated"] or 0)
        out.append({
            "role": r["role_name"],
            "total": int(r["total"] or 0),
            "members": members,
            "updated": updated,
            "notUpdated": members - updated,
        })
    return out


@router.get("/activity-summary")
def activity_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
) -> list[dict[str, Any]]:
    """The second Programmes card: one row per (role, programme) pairing
    `program_role` actually defines — not every role crossed with every
    programme, since not every programme applies to every role. `activity`
    in the response is `program.program_name`; `roleId`/`activityId` ride
    along so the member card below can ask for one pairing's leaders
    without re-deriving ids from the display names."""
    month_id = _month_id(year, month)
    rows = db.rows(
        f"""SELECT r.role_id, r.role_name, p.program_id, p.program_name,
                   SUM(l.is_deleted = 'N') AS total_members,
                   SUM(l.is_deleted = 'N' AND u.leader_id IS NOT NULL) AS updated
              FROM {config.PARTY_TRACK_DB}.program_role pr
              JOIN {config.PARTY_TRACK_DB}.role r ON r.role_id = pr.role_id
              JOIN {config.PARTY_TRACK_DB}.program p ON p.program_id = pr.program_id
              LEFT JOIN {config.PARTY_TRACK_DB}.leader l ON l.role_id = pr.role_id
              LEFT JOIN (
                    SELECT DISTINCT leader_id, program_id FROM {config.PARTY_TRACK_DB}.leader_program_activity
                     WHERE month_id = %s AND (is_deleted IS NULL OR is_deleted = 'N')
                   ) u ON u.leader_id = l.leader_id AND u.program_id = pr.program_id
             WHERE pr.is_deleted IS NULL OR pr.is_deleted = 'N'
             GROUP BY r.role_id, r.role_name, p.program_id, p.program_name
             ORDER BY (r.order_no IS NULL), r.order_no, r.role_name, p.program_name""",
        (month_id,),
    )
    out = []
    for r in rows:
        total_members = int(r["total_members"] or 0)
        updated = int(r["updated"] or 0)
        out.append({
            "role": r["role_name"],
            "roleId": r["role_id"],
            "activity": r["program_name"],
            "activityId": r["program_id"],
            "totalMembers": total_members,
            "updated": updated,
            "notUpdated": total_members - updated,
        })
    return out


@router.get("/leaders")
def program_leaders(
    role_id: int = Query(...),
    activity_id: int = Query(..., description="program_id, despite the name — matches activity-summary's field"),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
) -> list[dict[str, Any]]:
    """The third Programmes card: every active leader in one role, with
    their `leader_program_activity` participation for one programme and
    month — 0/0 for a leader who has not logged anything yet, not an
    absent row, so the card always has one row per roster member.

    `assembly`/`parliament` are `mytdp` tables, not `party_track` —
    `leader.constituency_id`/`parliament_id` reach across schemas the same
    way `config.PARTY_TRACK_DB`'s own docstring says the RDS user allows.
    Capped at `MAX_PAGE_SIZE` for the same reason the meeting member table is.
    """
    month_id = _month_id(year, month)
    rows = db.rows(
        f"""SELECT l.leader_id, l.leader_name, a.name AS assembly_name, p.parliament_name,
                   lpa.total AS participated, lpa.completed AS completed
              FROM {config.PARTY_TRACK_DB}.leader l
              LEFT JOIN assembly a ON a.id = l.constituency_id
              LEFT JOIN parliament p ON p.id = l.parliament_id
              LEFT JOIN {config.PARTY_TRACK_DB}.leader_program_activity lpa
                     ON lpa.leader_id = l.leader_id AND lpa.program_id = %s AND lpa.month_id = %s
                    AND (lpa.is_deleted IS NULL OR lpa.is_deleted = 'N')
             WHERE l.role_id = %s AND l.is_deleted = 'N'
             ORDER BY l.leader_name
             LIMIT %s""",
        (activity_id, month_id, role_id, config.MAX_PAGE_SIZE),
    )
    return [
        {
            "id": r["leader_id"],
            "mid": str(r["leader_id"]),
            "name": r["leader_name"] or "—",
            "parliament": r["parliament_name"] or "—",
            "assembly": r["assembly_name"] or "—",
            "participated": int(r["participated"] or 0),
            "completed": int(r["completed"] or 0),
        }
        for r in rows
    ]


@router.get("/{program_id}/daywise")
async def day_wise(program_id: str) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail=_UNWIRED)


@router.get("/{program_id}/attendees")
async def attendees(
    program_id: str,
    ac: str | None = Query(None, description="Constituency name, exact match"),
    limit: int = Query(config.MAX_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail=_UNWIRED)
