# Backend/queries.py — the SELECT statements the read endpoints are
# built on. Kept apart from the endpoints, same as ../admin-dashboard/Backend/queries.py.

# One round trip instead of three: total/active/inactive is the same COUNT the
# admin's reference queries run separately, collapsed into one row. Statement
# count is what costs against RDS here, not row count (see CLAUDE.md's note on
# db.py) — three SELECT COUNT(*) FROM user round trips would each pay the same
# ~220ms as this single one.
USER_STATS_SELECT = """
  SELECT COUNT(user_id) AS total,
         SUM(CASE WHEN is_enabled = 'Y' THEN 1 ELSE 0 END) AS active,
         SUM(CASE WHEN is_enabled = 'N' THEN 1 ELSE 0 END) AS inactive
  FROM user
"""

# Active-login count per team, for the Portal Dashboard's "Logins by Teams"
# panel (the live counterpart of the admin dashboard's per-role counts). Starts from
# team_type rather than user, same reasoning as the admin dashboard's buildRoles: a
# team with zero active logins should still appear with logins=0 rather than
# vanishing from the panel, so it's a LEFT JOIN with the is_enabled filter
# moved into the join condition, not a WHERE clause. 967 users currently carry
# a NULL team_type_id (no team assigned) and are excluded — there is no team
# row to attach them to.
TEAM_STATS_SELECT = """
  SELECT tt.team_type_id AS id, tt.type_name AS name, COUNT(u.user_id) AS logins
  FROM team_type tt
  LEFT JOIN user u ON u.team_type_id = tt.team_type_id AND u.is_enabled = 'Y'
  GROUP BY tt.team_type_id, tt.type_name
  ORDER BY logins DESC
"""

# Distinct-user grant count per entitlement, for the Portal Dashboard's "Most
# Granted Entitlements" panel — the live counterpart of the fake
# component/activity_member_component numbers the live Dashboard's own
# "Most Granted Components" panel shows (see CLAUDE.md: this view calls
# components "Entitlements"). COUNT(DISTINCT UGR.user_id) rather than a bare
# COUNT: a user can reach the same entitlement through more than one group,
# and that must count once, not once per path. ORDER BY user_count DESC (not
# entitlement_id) — this is a ranking panel, so the highest-grant entitlement
# has to sort first.
ENTITLEMENT_STATS_SELECT = """
  SELECT E.entitlement_id, E.entitlement_type,
         COUNT(DISTINCT UGR.user_id) AS user_count
  FROM entitlement E
  JOIN group_entitlement_relation GER ON E.entitlement_id = GER.entitlement_id
  JOIN group_entitlement GE ON GER.group_entitlement_id = GE.group_entitlement_id
  JOIN user_group_entitlement UGE ON GE.group_entitlement_id = UGE.group_entitlement_id
  JOIN user_groups UG ON UGE.user_group_id = UG.user_group_id
  JOIN user_group_relation UGR ON UG.user_group_id = UGR.user_group_id
  GROUP BY E.entitlement_id, E.entitlement_type
  ORDER BY user_count DESC
"""

# Whole-catalog count, for the "Total Entitlements" KPI tile. Deliberately
# separate from counting ENTITLEMENT_STATS_SELECT's rows: that query's inner
# joins mean an entitlement with zero grants produces no row, so its row
# count would undercount the catalog. This is a bare table count with no
# join, so a never-granted entitlement is still counted.
ENTITLEMENT_TOTAL_SELECT = "SELECT COUNT(entitlement_id) AS total_entitlement FROM entitlement"

# The whole catalog as a plain {id, name} picker list — Create Entitlement
# Group's "view entitlements" checklist. Same {id, name} aliasing the
# access-value lookups use, so the frontend doesn't need to know the raw
# column name either.
# Filtered to is_active='Y' — DESCRIBE entitlement carries that column
# (nullable enum('Y','N'), every live row currently 'Y'); insert_entitlement
# sets it explicitly on create so a brand-new row doesn't fall out of its own
# picker by defaulting to NULL.
ENTITLEMENTS_SELECT = "SELECT entitlement_id AS id, entitlement_type AS name FROM entitlement WHERE is_active='Y' ORDER BY entitlement_type"

# The whole group_entitlement catalog as a plain {id, name} picker list —
# Create User Group's "view entitlement groups" checklist. Unlike
# GROUP_ENTITLEMENT_ALL_DESCRIPTIONS_SELECT (deduped by name, for display),
# this is one row per group_entitlement_id — the real identity
# user_group_entitlement links against, and descriptions do repeat (e.g.
# CAMPAIGN_DETAILS_UPDATE spans two ids), so deduping here would make a
# duplicate-named group unreachable.
# Filtered to is_active='Y', same reasoning and same guaranteed-set-on-insert
# as ENTITLEMENTS_SELECT above.
GROUP_ENTITLEMENTS_SELECT = (
    "SELECT group_entitlement_id AS id, description AS name FROM group_entitlement "
    "WHERE is_active='Y' ORDER BY description"
)

# The whole user_groups catalog as a plain {id, name} picker list — Assign
# User to User Groups' checklist (Entitlement Management). notes doubles as
# the display name, same as GROUP_ENTITLEMENTS_SELECT uses
# group_entitlement.description.
# Filtered to is_active='Y', same reasoning and same guaranteed-set-on-insert
# as ENTITLEMENTS_SELECT above.
USER_GROUPS_SELECT = "SELECT user_group_id AS id, notes AS name FROM user_groups WHERE is_active='Y' ORDER BY notes"

# Every user_group that already carries a given group_entitlement — lets
# "Assign user groups to entitlement group" (Entitlement Management) mark
# existing membership on its user-groups picker before the admin adds more,
# same distinction USER_ENTITLEMENTS_SELECT backs on Assign User to User
# Groups' own picker. Scoped with `WHERE group_entitlement_id = %s`.
GROUP_ENTITLEMENT_USER_GROUPS_SELECT = "SELECT user_group_id FROM user_group_entitlement WHERE group_entitlement_id = %s"

# The reverse lookup — every group_entitlement a given user_group already
# carries. Same table as GROUP_ENTITLEMENT_USER_GROUPS_SELECT, just scoped
# the other way; services.assign_group_entitlements runs this same SELECT
# inline to dedupe its INSERT, this is that query made reusable for "Assign
# entitlement groups to user group"'s picker.
# Joined into group_entitlement for the description, same reason
# USER_GROUP_USERS_SELECT joins `user` for username: Create User Group's "view
# existing groups" expand needs names to show, not bare ids. The id-only
# consumer (the picker's already-attached marking) ignores the extra column.
USER_GROUP_GROUP_ENTITLEMENTS_SELECT = """
  SELECT UGE.group_entitlement_id, GE.description
  FROM user_group_entitlement UGE
  JOIN group_entitlement GE ON UGE.group_entitlement_id = GE.group_entitlement_id
  WHERE UGE.user_group_id = %s
  ORDER BY GE.description
"""

# Every user already in a given user_group — the same "already there, mark it
# orange" role GROUP_ENTITLEMENT_USER_GROUPS_SELECT plays one join over, now
# for "Assign User Groups to User"'s reverse picker (pick one user_group,
# multi-select users to attach it to). Scoped with `WHERE user_group_id = %s`,
# same table assign_user_groups/revoke_user_group (services.py) already write.
# Joined into `user` for username (Assign User to User Groups' own "view
# contents" expand needs a name to show, not just the bare id) — the extra
# columns are ignored by the id-only consumer above.
USER_GROUP_USERS_SELECT = """
  SELECT UGR.user_id, u.username
  FROM user_group_relation UGR
  JOIN user u ON UGR.user_id = u.user_id
  WHERE UGR.user_group_id = %s
  ORDER BY u.username
"""

# Every entitlement a given group_entitlement bundles, straight off
# group_entitlement_relation — the catalog-only join
# GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT's own comment contrasts itself with,
# not filtered through any team/user reachability chain. Lets a picker show
# what's actually inside a group_entitlement (Portal User Detail's "Add
# entitlement groups", Entitlement Management's own pickers) before an admin
# commits to adding it, even for a bundle nobody's been granted yet.
GROUP_ENTITLEMENT_CATALOG_ENTITLEMENTS_SELECT = """
  SELECT E.entitlement_id, E.entitlement_type
  FROM group_entitlement_relation GER
  JOIN entitlement E ON GER.entitlement_id = E.entitlement_id
  WHERE GER.group_entitlement_id = %s
  ORDER BY E.entitlement_type
"""

# The reverse of GROUP_ENTITLEMENT_CATALOG_ENTITLEMENTS_SELECT — every
# group_entitlement a given entitlement is already bundled into. Backs Create
# Entitlement Group's own "View entitlements" checklist: an eye icon on each
# entitlement row shows which group(s) it already belongs to before an admin
# adds it to (or builds) another one. Same catalog-only join, unfiltered by
# team/user reachability.
ENTITLEMENT_GROUP_ENTITLEMENTS_SELECT = """
  SELECT GE.group_entitlement_id, GE.description
  FROM group_entitlement_relation GER
  JOIN group_entitlement GE ON GER.group_entitlement_id = GE.group_entitlement_id
  WHERE GER.entitlement_id = %s
  ORDER BY GE.description
"""

# One row per (group_entitlement, entitlement) an actual team-assigned user
# reaches — team_type/user/user_group_relation/user_groups/user_group_
# entitlement all inner-joined, same chain TEAM_STATS_SELECT's own "967
# users with no team_type_id are excluded" reasoning follows, just carried
# one step further out to the entitlements they hold. Unlike a catalog-only
# join (group_entitlement straight to group_entitlement_relation), this
# drops any bundle content nobody with a team has actually been granted —
# by product decision, not an oversight: a group_entitlement's catalog
# contents and what's genuinely reachable through a live team/user path can
# differ, and this endpoint means the latter. DISTINCT because the same
# (group_entitlement, entitlement) pair is reached once per team/user/group
# path to it, and that must collapse to one row, not one per path.
GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT = """
  SELECT DISTINCT
         GE.group_entitlement_id, GE.description AS group_entitlement_description,
         E.entitlement_id, E.entitlement_type
  FROM team_type TT
  JOIN user U ON TT.team_type_id = U.team_type_id
  JOIN user_group_relation UGR ON U.user_id = UGR.user_id
  JOIN user_groups UG ON UGR.user_group_id = UG.user_group_id
  JOIN user_group_entitlement UGE ON UG.user_group_id = UGE.user_group_id
  JOIN group_entitlement GE ON UGE.group_entitlement_id = GE.group_entitlement_id
  JOIN group_entitlement_relation GER ON GE.group_entitlement_id = GER.group_entitlement_id
  JOIN entitlement E ON GER.entitlement_id = E.entitlement_id
  ORDER BY GE.group_entitlement_id, E.entitlement_id
"""

# Every group_entitlement.description in the catalog, deduped by name (the
# same description can span more than one group_entitlement_id —
# CAMPAIGN_DETAILS_UPDATE does, confirmed against live data). No join to
# entitlement/user/team at all on purpose: this is the complete name list
# the Group Entitlements "View All" page lists against, so a description
# GROUP_ENTITLEMENT_ENTITLEMENTS_SELECT's stricter team/user-reachable join
# excludes entirely still gets a row — routers/stats.py pairs this with that
# query's own counts and fills in 0 (the frontend's "No entitlements
# found." case) for whatever this list has that the other doesn't.
GROUP_ENTITLEMENT_ALL_DESCRIPTIONS_SELECT = "SELECT DISTINCT description FROM group_entitlement ORDER BY description"

# One row per (user_group, group_entitlement, entitlement) a single user
# reaches — the Portal User Detail screen's "Groups & Entitlements" card.
# Scoped with `WHERE U.user_id = %s`, not filtered by is_enabled: a user's
# grants are the same rows whether their account is active (is_enabled='Y')
# or inactive ('N') — deactivating a sign-in doesn't revoke the group/
# entitlement relationships, so this reads the same for both, and the screen
# shows the Active/Inactive state separately from user_row (services.py).
USER_ENTITLEMENTS_SELECT = """
  SELECT UG.user_group_id, UG.notes AS user_group_notes,
         GE.group_entitlement_id, GE.description AS group_entitlement_description,
         E.entitlement_id, E.entitlement_type
  FROM user U
  JOIN user_group_relation UGR ON U.user_id = UGR.user_id
  JOIN user_groups UG ON UGR.user_group_id = UG.user_group_id
  JOIN user_group_entitlement UGE ON UG.user_group_id = UGE.user_group_id
  JOIN group_entitlement GE ON UGE.group_entitlement_id = GE.group_entitlement_id
  JOIN group_entitlement_relation GER ON GE.group_entitlement_id = GER.group_entitlement_id
  JOIN entitlement E ON GER.entitlement_id = E.entitlement_id
  WHERE U.user_id = %s
  ORDER BY UG.user_group_id, GE.group_entitlement_id, E.entitlement_id
"""

# One row per distinct entitlement a user reaches through any group_entitlement
# path — the sidebar menu's source list, unlike USER_ENTITLEMENTS_SELECT's
# (user_group, group_entitlement, entitlement) grid, which is the Detail
# screen's "Groups & Entitlements" card and deliberately keeps every path
# separate so a revoke can target one group at a time. GROUP BY on
# entitlement_id alone collapses an entitlement reached through more than one
# group_entitlement/user_group to one row, since which path granted it doesn't
# matter for "does this menu item show." Same not-filtered-by-is_enabled
# reasoning as USER_ENTITLEMENTS_SELECT: deactivating a login doesn't revoke
# the underlying grants; callers gate the menu on the account's active state
# separately if they need to.
# E.is_active='Y', same catalog filter ENTITLEMENTS_SELECT uses — a
# deactivated entitlement drops off the menu even if the user's grant row is
# still there, since the grant isn't what decides whether the item still
# exists to navigate to.
USER_ENTITLEMENT_MENU_SELECT = """
  SELECT E.entitlement_id, E.entitlement_type AS entitlement_name
  FROM user_group_relation UGR
  JOIN user_group_entitlement UGE ON UGR.user_group_id = UGE.user_group_id
  JOIN group_entitlement_relation GER ON UGE.group_entitlement_id = GER.group_entitlement_id
  JOIN entitlement E ON GER.entitlement_id = E.entitlement_id
  WHERE UGR.user_id = %s AND E.is_active = 'Y'
  GROUP BY E.entitlement_id
  ORDER BY entitlement_name
"""

# One row per login, for the "View logins" drill-down behind each team in the
# Logins by Teams panel, and behind the Active/Inactive Users KPI tiles.
# Fetched once per status and filtered on the frontend, same reasoning as
# the admin dashboard's /api/members: one round trip for the whole set beats one
# query per click. No WHERE/ORDER here — TEAM_USER_STATUS_FILTER supplies
# both, since inactive is 75k+ rows and needs `ORDER BY user_id` (cheap, uses
# the primary key) rather than `team_type_id` (would need a real sort).
#
# Inactive is capped at 500 (below) — measured directly against this table:
# EXPLAIN shows every join already resolving through a primary key (eq_ref),
# so the ~9s/~18MB cost for the full 75,128 rows is network transfer of that
# much row data to this dev box, not an inefficient query — a bare 1-column
# SELECT over the same WHERE is 1.6s, and just adding the real columns (no
# joins yet) is 7.5s. No amount of indexing shrinks that; only fetching less
# data does. Active has no such cap — it's only ~1,655 rows (~0.4s) and the
# Logins by Teams drill-down needs the complete set to filter accurately.
#
# access_value is an untyped id whose meaning depends on access_type — same
# shape as the admin dashboard's activity_location_value, resolved the same way
# (a CASE across the candidate lookup tables, joined once each). MLA and MP
# both point into `constituency` (an assembly and a parliamentary seat can't
# collide since it's one shared id space); DISTRICT into `district`; ZONE into
# `zone`; STATE into `state` (seen as both 1 "Andhra Pradesh" and 24 "Tamil
# Nadu" — not a constant, unlike the admin dashboard's STATE level which always
# resolves to 'AP'). Falls back to the raw id for any other/NULL access_type
# rather than dropping it. access_value_id is the same column, unresolved —
# the Detail screen's access-value picker needs the real id to preselect the
# current option, not the display name the CASE produces.
TEAM_USERS_SELECT = """
  SELECT u.user_id, u.username, u.is_enabled,
         CONCAT_WS(' ', u.firstname, u.lastname) AS person_name,
         u.mobile, u.registered_time,
         u.team_type_id, tt.type_name AS team_type,
         u.access_type, u.access_value AS access_value_id,
         CASE u.access_type
           WHEN 'MLA' THEN c.name
           WHEN 'MP' THEN c.name
           WHEN 'DISTRICT' THEN d.district_name
           WHEN 'ZONE' THEN z.zone_name
           WHEN 'STATE' THEN s.state_name
           ELSE u.access_value
         END AS access_value
  FROM user u
  LEFT JOIN team_type tt ON u.team_type_id = tt.team_type_id
  LEFT JOIN constituency c ON c.constituency_id = u.access_value AND u.access_type IN ('MLA', 'MP')
  LEFT JOIN district d ON d.district_id = u.access_value AND u.access_type = 'DISTRICT'
  LEFT JOIN zone z ON z.zone_id = u.access_value AND u.access_type = 'ZONE'
  LEFT JOIN state s ON s.state_id = u.access_value AND u.access_type = 'STATE'
"""

TEAM_USER_STATUS_FILTER = {
    "active": " WHERE u.is_enabled = 'Y' ORDER BY u.team_type_id, u.user_id",
    "inactive": " WHERE u.is_enabled = 'N' ORDER BY u.user_id LIMIT 500",
    "all": " ORDER BY u.user_id",
}

# Find a User (Portal Dashboard) — one column per searchable field, substring
# matched. None of mobile/firstname/lastname are indexed and username's
# idx_user_username can't help a substring match either, so every keystroke is
# a full 76,783-row scan — measured directly at ~0.25-0.28s each (username
# range-scans its index in ~0.22s), comfortably inside the 300ms debounce the
# frontend already uses for the admin cadre lookups this mirrors. user_id is
# the PK, but a LIKE %term% still can't range-scan it (the wildcard is on
# both sides) — same table-scan cost as the other three, not a shortcut.
TEAM_USER_SEARCH_COLUMN = {
    "username": "u.username",
    "mobile": "u.mobile",
    "name": "CONCAT_WS(' ', u.firstname, u.lastname)",
    "userid": "u.user_id",
}

TEAM_USER_SEARCH_STATUS_FILTER = {
    "active": " AND u.is_enabled = 'Y'",
    "inactive": " AND u.is_enabled = 'N'",
    "all": "",
}

# One row for the post-write re-read, same shape TEAM_USERS_SELECT already
# produces — the Detail screen's Save applies against this.
TEAM_USER_BY_ID_SELECT = TEAM_USERS_SELECT + " WHERE u.user_id = %s"

# Access-value pickers, one per access_type — the Detail screen's second
# dropdown. Same real AP data the admin dashboard's own constituency/parliament
# lookups use (identical queries — see ../admin-dashboard/Backend/routers/lookups.py),
# reused here rather than re-derived; district/zone/state don't exist on the
# admin side. AP-scoped (state_id = 1) for constituencies and districts —
# every DISTRICT-type access_value seen in the live data was an AP district,
# so a non-AP one isn't representable in this picker yet. All five aliased to
# {id, name} so the frontend doesn't need to know which raw column is which.
CONSTITUENCIES_SELECT = """
  SELECT constituency_id AS id, name FROM constituency
  WHERE state_id = 1 AND deform_date IS NULL AND election_scope_id = 2
  GROUP BY constituency_id
"""
PARLIAMENTS_SELECT = """
  SELECT C.constituency_id AS id, C.name
  FROM constituency C
  JOIN election E ON E.election_scope_id = C.election_scope_id
  WHERE C.election_scope_id = 1 AND C.state_id = 1
    AND E.election_year = 2024 AND C.deform_date IS NULL
"""
DISTRICTS_SELECT = "SELECT district_id AS id, district_name AS name FROM district WHERE state_id = 1 ORDER BY district_name"
ZONES_SELECT = "SELECT zone_id AS id, zone_name AS name FROM zone ORDER BY zone_name"
# AP-scoped like the two above, by product decision — the `state` table has 87
# rows (every Indian state plus a run of US states used elsewhere in this
# schema), and TEAM_USERS_SELECT's CASE can still resolve a legacy non-AP
# STATE row (e.g. state_id 24, Tamil Nadu) to its real name for display; this
# picker just doesn't offer choosing one going forward.
STATES_SELECT = "SELECT state_id AS id, state_name AS name FROM state WHERE state_id = 1"
