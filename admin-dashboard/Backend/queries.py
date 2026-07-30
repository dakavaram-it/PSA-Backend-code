# Backend/queries.py — the SELECT statements the read endpoints are built on.
# Kept apart from the endpoints so the query rationale (and its gotchas) lives
# in one place.
from config import CADRE_IMAGE_BASE

# One row per login. Collapses the member x role x component fan-out in SQL.
# location_name resolves location_value (an untyped int) to a human name: 'AP'
# for level 2 (state-wide), else the matching constituency.name for level 4/5
# (empty string otherwise).
#
# The "a login has at most one active access_level grant" assumption the
# collapsed level/location columns were written against is false: 99 logins
# currently hold more than one (an ASSEMBLY seat plus a PARLIAMENT seat, say).
# Taking MAX() of each column independently mixed rows together — member 115
# holds (ASSEMBLY, 173) and (PARLIAMENT, 510) and was reported as
# (ASSEMBLY, 510), a pairing that exists in no grant row. The ROW_NUMBER()
# derived table below picks one real grant per login instead, ordered by
# primary key so it is both deterministic and the same grant the old list query
# happened to surface. The MAX() wrappers stay only to satisfy GROUP BY — the
# join now yields at most one access_level row per login.
# Callers that need every active scope should use `locations`
# (MEMBER_LOCATIONS_QUERY), which is what the Detail screen reads; these
# columns remain a one-line summary.
#
# The joins are LEFT JOINs rather than inner ones — with inner joins, any login
# with zero granted components, or no active role, would vanish from the
# Active/Inactive counts entirely instead of just showing an empty
# role/component list, which would quietly undercount both KPIs. Same reasoning
# for TC: LEFT JOIN so a login whose tdp_cadre_id doesn't resolve still counts.
#
# The membership_id '#' prefix is applied here so a row returned by a *write*
# endpoint matches the one in the list.
MEMBER_SELECT = f"""
  SELECT AM.activity_member_id, AM.member_name, AM.tdp_cadre_id, AM.inserted_time,
         AM.updated_by, AM.is_acitve, CONCAT("#", TC.membership_id) AS membership_id, TC.mobile_no,
         CONCAT("{CADRE_IMAGE_BASE}", TC.image) AS image_url,
         MAX(AMAT.user_type_id) AS role_id, MAX(UT.type) AS role_name, MAX(UT.short_name) AS role_short,
         MAX(AMAL.activity_member_level_id) AS level_id, MAX(UL.level) AS level_name,
         MAX(AMAL.activity_location_value) AS location_value,
         MAX(CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
                  WHEN AMAL.activity_member_level_id = 4 THEN PC.name
                  WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END) AS location_name,
         GROUP_CONCAT(DISTINCT AMC.component_id ORDER BY AMC.component_id) AS component_ids
  FROM activity_member AM
  LEFT JOIN tdp_cadre TC ON TC.tdp_cadre_id = AM.tdp_cadre_id
  LEFT JOIN activity_member_access_type AMAT ON AMAT.activity_member_id = AM.activity_member_id AND AMAT.is_active='Y'
  LEFT JOIN user_type UT ON UT.user_type_id = AMAT.user_type_id
  LEFT JOIN (
      SELECT activity_member_id, activity_member_level_id, activity_location_value,
             ROW_NUMBER() OVER (PARTITION BY activity_member_id
                                ORDER BY activity_member_access_level_id) AS rn
      FROM activity_member_access_level WHERE is_active='Y'
  ) AMAL ON AMAL.activity_member_id = AM.activity_member_id AND AMAL.rn = 1
  LEFT JOIN user_level UL ON UL.user_level_id = AMAL.activity_member_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  LEFT JOIN activity_member_component AMC ON AMC.activity_member_id = AM.activity_member_id AND AMC.is_valid='Y'
"""

GROUP_BY = """ GROUP BY AM.activity_member_id, AM.member_name, AM.tdp_cadre_id,
  AM.inserted_time, AM.updated_by, AM.is_acitve, TC.membership_id, TC.mobile_no, TC.image"""

# The status filter is applied in SQL rather than by filtering the full list in
# Python, so a filtered call doesn't drag every login across the wire to throw
# most of them away.
STATUS_FILTER = {"active": " WHERE AM.is_acitve = 'Y'", "inactive": " WHERE AM.is_acitve = 'N'"}

# A login can hold more than one active access_level grant at once (e.g. an
# ASSEMBLY seat plus a PARLIAMENT seat). MEMBER_SELECT collapses that to a
# single MAX()'d level/location for backward compat, but the Detail screen
# wants every active location, so this fetches them separately and gets
# attached as `locations`.
MEMBER_LOCATIONS_QUERY = """
  SELECT AMAL.activity_member_id,
         AMAL.activity_member_level_id AS level_id, UL.level AS level_name,
         AMAL.activity_location_value AS location_value,
         CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
              WHEN AMAL.activity_member_level_id = 4 THEN PC.name
              WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END AS location_name
  FROM activity_member_access_level AMAL
  LEFT JOIN user_level UL ON UL.user_level_id = AMAL.activity_member_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  WHERE AMAL.is_active = 'Y'
"""

# Cadre lookup by mobile number (create-flow step 1, read-only).
# mobile_no is indexed but NOT unique — multiple cadre (e.g. family members
# sharing one phone) can share a number, so this returns every match (possibly
# []). Column aliases are kept as given by the admin's reference queries (not
# this codebase's usual snake_case) so the response stays recognisable against
# the source SQL. Things worth knowing:
#   - AM only joins on is_acitve='Y', so a *deactivated* login reads identical
#     to "no login yet" here (AMID/LOCLEVEL/TEAMNAME all null) — unlike
#     create_member's duplicate check, which still counts a deactivated
#     activity_member row as "already exists".
#   - No GROUP BY: a cadre with more than one active role or access-level grant
#     at once (rare in this data) fans out into multiple rows.
#   - LOCATION resolves LOCVALUE (an untyped int) to a human name. PC/AC are
#     two separate joins to the same table (one per level) so the CASE can pick
#     the right one without ambiguity.
# is_deleted='N' was added (not in the source query) so a deleted cadre record
# can't show up here and then 404 when picked for creation.
CADRE_BY_MOBILE_SELECT = f"""
  SELECT
      CR.tdp_cadre_id AS CADREID,
      UPPER(CR.first_name) AS MEMBERNAME,
      CR.mobile_no AS MOBILENO,
      CONCAT('#', CR.membership_id) AS MID,
      CONCAT("{CADRE_IMAGE_BASE}", CR.image) AS IMAGE,
      AM.activity_member_id AS AMID,
      UL.level AS LOCLEVEL,
      AMAL.activity_location_value AS LOCVALUE,
      CASE WHEN AMAL.activity_member_level_id = 2 THEN 'AP'
           WHEN AMAL.activity_member_level_id = 4 THEN PC.name
           WHEN AMAL.activity_member_level_id = 5 THEN AC.name ELSE '' END AS LOCATION,
      CONCAT('#', LOD.otp) AS OTP,
      CONCAT('#', DATE(LOD.generated_time)) AS EXPDATE,
      UT.short_name AS TEAMNAME
  FROM tdp_cadre CR
  LEFT JOIN activity_member AM ON CR.tdp_cadre_id = AM.tdp_cadre_id AND AM.is_acitve = 'Y' AND AM.activity_member_id <> 581
  LEFT JOIN activity_member_access_level AMAL ON AM.activity_member_id = AMAL.activity_member_id AND AMAL.is_active = 'Y'
  LEFT JOIN user_level UL ON AMAL.activity_member_level_id = UL.user_level_id
  LEFT JOIN constituency PC ON AMAL.activity_location_value = PC.constituency_id AND AMAL.activity_member_level_id = 4
  LEFT JOIN constituency AC ON AMAL.activity_location_value = AC.constituency_id AND AMAL.activity_member_level_id = 5
  LEFT JOIN activity_member_access_type AMAT ON AM.activity_member_id = AMAT.activity_member_id AND AMAT.is_active = 'Y'
  LEFT JOIN user_type UT ON AMAT.user_type_id = UT.user_type_id
  LEFT JOIN login_otp_details LOD ON CR.tdp_cadre_id = LOD.tdp_cadre_id AND LOD.is_valid = 'Y'
  WHERE CR.mobile_no = %s AND CR.is_deleted = 'N'
  ORDER BY CR.tdp_cadre_id, UL.user_level_id, UT.order_no
"""

# Access-type grant count for a MID (read-only, standalone check — NOT used by
# MEMBER_SELECT). That query collapses activity_member_access_type with MAX()
# on the assumption a login has at most one active role grant at a time. This
# exists purely to check that assumption for a given MID by listing every
# access_type row (active or not) tied to it, instead of trusting the
# aggregated columns.
ACCESS_TYPES_BY_MID_SELECT = """
  SELECT AMAT.activity_member_access_type_id, AMAT.activity_member_id,
         AMAT.user_type_id, UT.type AS role_name, UT.short_name AS role_short,
         AMAT.is_active
  FROM tdp_cadre TC
  JOIN activity_member AM ON AM.tdp_cadre_id = TC.tdp_cadre_id
  JOIN activity_member_access_type AMAT ON AMAT.activity_member_id = AM.activity_member_id
  LEFT JOIN user_type UT ON UT.user_type_id = AMAT.user_type_id
  WHERE TC.membership_id = %s
  ORDER BY AM.activity_member_id, AMAT.is_active DESC, AMAT.user_type_id
"""
