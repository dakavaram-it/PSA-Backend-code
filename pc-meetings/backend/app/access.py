"""Which assemblies a caller may see, and the SQL that narrows each table to them.

Every figure this service reports is counted in SQL over the whole state. A user
is granted a subset of assemblies by the portal (`user_state_access_info` /
`user_constituency_access_info` in `dakavara_pa`), and this module is what turns
that grant into a predicate on each table the meetings and programmes routes
count over.

**`mytdp.assembly.id` and `dakavara_pa.constituency.constituency_id` are the same
id.** Verified directly: all 175 assembly rows match a constituency row on id
*and* name, none missing. That is what lets a grant resolved in one schema be
applied in the other without a name-matching table in between.

How each table reaches an assembly — none of this is uniform, so a caller has to
say which table and (for the two schedule tables) which level it is filtering:

* `meeting_invitee.constituency_id` — the assembly, on the row. 491,620/491,620
  populated.
* `meeting_schedules` — `assembly_id` is the assembly at Unit and Mandal level
  (checked against the booth roll: 2,860/2,860 of meeting 22's rows agree), but
  at AC level `entity_id` is the assembly and `assembly_id` disagrees with it on
  12 of 178 rows, and at PC level `assembly_id` is 0 or NULL. So AC reads
  `entity_id` and PC expands the parliament to its assemblies.
* `meeting_conducted_status` has no assembly column at all: `location_id` is a
  unit, an assembly, a parliament or a mandal committee depending on the
  meeting's level, so each level joins its own way — and Mandal has no join at
  all (`dakavara_pa` shares no id space with `mytdp` here), so it filters
  against the enrolled committee roster's own `constituency_id`.
* `meeting_attendance` and `meeting_resolutions` carry neither, but both carry
  `schedule_id`/`scheduled_id` into `meeting_schedules` — 100% populated on
  every meeting — so they inherit whatever that row is scoped to.
* `party_track.leader.constituency_id` is the assembly, the same id again.

The id lists are interpolated rather than bound: they come off the database as
integers and are re-checked as such in `_ids`, and threading a variable-length
`%s` list through the format strings these routes build would mean carrying the
values through every `.format()` in the file. `programs.py` already builds its
`role_id IN (...)` list the same way.
"""

from __future__ import annotations

from . import config

# A predicate that can never match — what an empty grant has to mean. `IN ()` is
# a syntax error in MySQL, so an empty list cannot simply be interpolated.
NOTHING = "0 = 1"


class Scope:
    """The assemblies one caller may see. `ids is None` means the whole state."""

    __slots__ = ("ids",)

    def __init__(self, ids: list[int] | None) -> None:
        self.ids = None if ids is None else sorted({int(i) for i in ids})

    @property
    def unrestricted(self) -> bool:
        return self.ids is None

    @property
    def nums(self) -> str:
        """`108, 109` — for the integer-typed columns."""
        return ", ".join(str(i) for i in self.ids or ())

    @property
    def strs(self) -> str:
        """`'108', '109'` — for the varchar-typed ones (`assembly.id`,
        `meeting_conducted_status.location_id`, …). Comparing those against
        numbers instead would make MySQL cast the column and drop its index."""
        return ", ".join(f"'{i}'" for i in self.ids or ())


# The whole state, for anything that runs outside a request (there is nothing
# today) and for a user whose grant covers the state.
ALL = Scope(None)


def _clause(scope: Scope, sql: str) -> str:
    """`sql` when the scope restricts anything, `1 = 1` when it does not.

    Always a complete boolean expression, so callers can drop it into a WHERE
    with `AND` and never have to test the scope themselves.
    """
    if scope.unrestricted:
        return "1 = 1"
    return NOTHING if not scope.ids else sql


def invitee(scope: Scope, alias: str = "i") -> str:
    """`meeting_invitee` — the assembly is on the row."""
    return _clause(scope, f"{alias}.constituency_id IN ({scope.nums})")


def schedules(scope: Scope, level: str, alias: str = "s") -> str:
    """`meeting_schedules`, for a meeting at `level` (adapt.level_code's codes).

    See the module docstring for why the column differs by level.
    """
    if level == "AC":
        sql = f"CAST({alias}.entity_id AS CHAR) IN ({scope.strs})"
    elif level == "PC":
        sql = (
            f"EXISTS (SELECT 1 FROM assembly sa WHERE sa.parliament_id = "
            f"CAST({alias}.entity_id AS CHAR) AND sa.id IN ({scope.strs}))"
        )
    else:  # Unit, Mandal — both carry the assembly on the schedule row itself
        sql = f"{alias}.assembly_id IN ({scope.nums})"
    return _clause(scope, sql)


def conducted(scope: Scope, level: str, alias: str = "mcs") -> str:
    """`meeting_conducted_status`, for a meeting at `level`.

    `location_id` is a varchar holding whatever the level's location is. Mandal
    resolves through the enrolled committee roster rather than a join — that
    roster lives in `dakavara_pa` and shares no id space with `mytdp` — so the
    allowed location ids are listed out; it is a few hundred rows state-wide.
    """
    if level == "AC":
        sql = f"{alias}.location_id IN ({scope.strs})"
    elif level == "PC":
        sql = (
            f"EXISTS (SELECT 1 FROM assembly sa WHERE sa.parliament_id = "
            f"{alias}.location_id AND sa.id IN ({scope.strs}))"
        )
    elif level == "Mandal":
        allowed = mandal_location_ids(scope)
        sql = f"{alias}.location_id IN ({allowed})" if allowed else NOTHING
    else:  # Unit — through the live booth roll, the same pairing /api/units counts
        sql = (
            f"EXISTS (SELECT 1 FROM booth sb WHERE sb.unit_id = {alias}.location_id "
            f"AND sb.publication_id = {config.UNIT_PUBLICATION_ID} "
            f"AND sb.assembly_id IN ({scope.strs}))"
        )
    return _clause(scope, sql)


def mandal_location_ids(scope: Scope) -> str:
    """`'1401', '1402'` — the enrolled Mandal/Town/Division committees inside
    this scope, quoted for the varchar columns that hold them. Empty string when
    the scope allows none, which callers turn into `NOTHING` themselves (an
    empty `IN ()` will not parse)."""
    from .routers.committees import locations

    allowed = {str(i) for i in (scope.ids or ())}
    return ", ".join(
        f"'{r['location_id']}'"
        for r in locations()
        if r["location_id"] is not None
        and (scope.unrestricted or str(r["constituency_id"]) in allowed)
    )


def leader(scope: Scope, alias: str = "l") -> str:
    """`party_track.leader` — `constituency_id` is the assembly, same id again."""
    return _clause(scope, f"{alias}.constituency_id IN ({scope.nums})")


def via_schedule(scope: Scope, level: str, alias: str, column: str = "schedule_id") -> str:
    """A table that reaches an assembly only through its `meeting_schedules` row
    — `meeting_attendance.schedule_id` and `meeting_resolutions.scheduled_id`,
    both 100% populated on every meeting that has rows at all.

    An EXISTS rather than a join, so an unrestricted caller's query keeps the
    exact shape (and the exact counts) it had before there was a scope: a join
    would drop any row whose schedule id no longer resolves.
    """
    return _clause(
        scope,
        f"EXISTS (SELECT 1 FROM meeting_schedules xs WHERE xs.id = {alias}.{column} "
        f"AND {schedules(scope, level, 'xs')})",
    )


def via_conducted(scope: Scope, level: str, alias: str = "mr") -> str:
    """`meeting_remark`, which carries no location of its own — it hangs off the
    PC in-charge's `meeting_conducted_status` row, which does."""
    return _clause(
        scope,
        f"EXISTS (SELECT 1 FROM meeting_conducted_status xc "
        f"WHERE xc.meeting_conducted_status_id = {alias}.meeting_conducted_status_id "
        f"AND {conducted(scope, level, 'xc')})",
    )


def feedback(scope: Scope, alias: str = "f") -> str:
    """`feedback_comment` — shared with the rest of the party estate and carrying
    no usable assembly of its own (`constituency_id` is empty on every committee-
    meeting row), so a capture belongs to whichever assembly its invitee is in."""
    return _clause(
        scope,
        f"EXISTS (SELECT 1 FROM meeting_invitee fi WHERE fi.meeting_id = {alias}.program_id "
        f"AND fi.membership_id = {alias}.membership_id AND {invitee(scope, 'fi')})",
    )


# --- Rosters ---------------------------------------------------------------
# The "not scheduled" / "never updated" figures measure a meeting against the
# full roster of locations at its level, so the roster itself has to shrink with
# the scope or those counts would report work in assemblies the caller cannot see.


def booth(scope: Scope, alias: str = "b") -> str:
    """`booth` — the live electoral roll, and the only trustworthy path from a
    unit to its assembly (`unit.assembly_id` is not relied on anywhere here)."""
    return _clause(scope, f"{alias}.assembly_id IN ({scope.strs})")


def assembly(scope: Scope, alias: str = "a") -> str:
    return _clause(scope, f"{alias}.id IN ({scope.strs})")


def parliament(scope: Scope, alias: str = "p") -> str:
    """A parliament is in scope when any of its assemblies is — a PC straddling
    the edge of a grant counts as one whole PC, since a PC-level meeting has one
    row for it and there is no half of one to report."""
    return _clause(
        scope,
        f"EXISTS (SELECT 1 FROM assembly pa WHERE pa.parliament_id = {alias}.id "
        f"AND pa.id IN ({scope.strs}))",
    )


# --- Queries spanning more than one level ----------------------------------
# `meeting_levels.level_name`, by the code `adapt.level_code` maps it to.
# Anything unrecognised falls through to Unit there, so the Unit branch below is
# written as "none of the other three" rather than an equality, to match.
_LEVEL_NAMES = {
    "Unit": "Unit Level",
    "Mandal": "Mandal / Town / Division",
    "AC": "AC",
    "PC": "PC",
}


def _spanning(scope: Scope, predicate, level_col: str) -> str:
    """One predicate for a query whose rows may come from meetings at different
    levels — the drill-downs take a list of meeting ids and do not group them —
    applying each level's own rule, selected by the row's own `level_name`."""
    if scope.unrestricted:
        return "1 = 1"
    named = ("Mandal", "AC", "PC")
    parts = [
        f"({level_col} = '{_LEVEL_NAMES[code]}' AND {predicate(code)})" for code in named
    ]
    others = ", ".join(f"'{_LEVEL_NAMES[code]}'" for code in named)
    parts.append(
        f"(({level_col} IS NULL OR {level_col} NOT IN ({others})) AND {predicate('Unit')})"
    )
    return "(" + " OR ".join(parts) + ")"


def schedules_spanning(scope: Scope, alias: str = "s", level_col: str = "ml.level_name") -> str:
    return _spanning(scope, lambda level: schedules(scope, level, alias), level_col)


def conducted_spanning(scope: Scope, alias: str = "mcs", level_col: str = "ml.level_name") -> str:
    return _spanning(scope, lambda level: conducted(scope, level, alias), level_col)
