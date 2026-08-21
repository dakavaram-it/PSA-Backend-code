# Backend/queries.py — every SELECT behind Dashboard 2, in one place.
#
# Dashboard 2 reads the same seven tables the frontend mockup names:
#   main_election_type       the five body groups the screen is split into
#   proposal_election_type   the election type, now carrying main_election_type_id
#   proposal_role            the post
#   constituency_reservation the seat's reservation
#   proposal_position        one post at one location — Dashboard 2's "location" row
#   proposal_candidate       the names proposed for it
#   proposal_status          1 Proposed, 2 Confirmed
#
# ---------------------------------------------------------------------------
# What a "position row" and a "location" are here
# ---------------------------------------------------------------------------
# Dashboard 2's table has one row per POST (MPTC, Sarpanch, Mayor, ...) inside a BODY
# (Mandal Parishad, Gram Panchayat, ...), and each such row counts LOCATIONS.
#
# A post is NOT identified by proposal_role_id alone. Role 5 (Corporator) is used both for
# Municipal Ward seats (body 4) and Corporation Ward seats (body 5), so a role-only grouping
# would merge Dashboard 2's "Ward Councillor" row into its "Corporator" row. The identity is
# the triple (main_election_type_id, proposal_election_type_id, proposal_role_id), and every
# endpoint that names one position takes all three.
#
# A location is one proposal_position row. Its reservation, its names and its stage all hang
# off that row, which is what makes the location list, the comparison table and the counters
# agree without a second definition anywhere.
#
# ---------------------------------------------------------------------------
# The stage ladder
# ---------------------------------------------------------------------------
# Dashboard 2 draws seven stages. Only the first four exist in this database:
#
#   0 Not started        no active proposal_candidate
#   1 Proposal received  >= 1 active candidate
#   2 Confirmed          >= 1 candidate at proposal_status_id = 2
#   3 Nomination filed   >= 1 candidate with is_nominated = 'Y'
#   4 Door to Door       no table
#   5 Door to Door - 2   no table
#   6 Result declared    no table
#
# Stages 4-6 are served as hard zeros from routers/dashboard.py, not from SQL — see
# EMPTY_STAGE_FIELDS there. Keeping them out of the SQL is deliberate: when the tables
# arrive, the change is one join here and one deleted constant there, and nothing in
# between has to be re-derived.
#
# Stage 1 is "has an active candidate", NOT "proposal_position.started_time IS NOT NULL".
# The two disagree in the live data (MPTC: 11 active candidates against 2 stamped
# started_time values), and Dashboard 2's own chip reads "at least one name received", so
# the candidate rows are the truth. started_time is still returned per location as
# `started_time`, for anyone who needs the stamp itself.
#
# proposal_position.proposal_status_id exists but is NULL on all 43,636 rows, so nothing
# here reads it. The status that matters is proposal_candidate.proposal_status_id.

# ---------------------------------------------------------------------------
# Assembly attribution
# ---------------------------------------------------------------------------
# Which assembly a proposal_position sits in is not one column — the same three shapes
# ../../portal-frontend-code/Backend/main.py documents at assembly_match():
#
#   * mandal- and ward-level rows (MPTC, MPP, ZPTC, Municipal Ward, Corporation Ward)
#     name their parent assembly directly in user_address.constituency_id;
#   * Municipality / Corporation rows point constituency_id at the body's OWN constituency,
#     and reach the assembly only through assembly_local_election_body;
#   * Zilla Parishath rows have neither — a ZP is a district, and only district_id is set.
#
# That backend answers "does this position belong to assembly X?", which is a per-assembly
# test. Dashboard 2 needs the inverse — "which assembly does this position belong to?" —
# because it GROUPs BY assembly, and a position counted under two assemblies would make the
# geo table stop adding up to the position total, which is the one property that table
# promises.
#
# ponytail: a whole-body or district position is therefore attributed to exactly ONE
# assembly — the lowest-numbered one it touches. That is arbitrary for the ~350 rows it
# affects (115+115 Municipality, 12+12 Corporation, 26+26 Zilla Parishath) and exact for the
# other 43,286. If per-assembly precision is ever needed for those bodies, the fix is a
# proposal_position -> assembly bridge table, not a second expression here.
ASSEMBLY_EXPR = """
  CASE
    WHEN UA.constituency_id IS NOT NULL AND UA.constituency_id <> PCon.constituency_id
      THEN UA.constituency_id
    WHEN UA.local_election_body IS NOT NULL THEN (
      SELECT MIN(AL.constituency_id) FROM assembly_local_election_body AL
      WHERE AL.local_election_body_id = UA.local_election_body)
    WHEN UA.district_id IS NOT NULL THEN (
      SELECT MIN(A2.constituency_id) FROM constituency A2
      JOIN election_scope ES2 ON A2.election_scope_id = ES2.election_scope_id
      WHERE ES2.election_type_id = 2 AND A2.deform_date IS NULL
        AND A2.district_id = UA.district_id)
  END
"""

# Pre-aggregated candidate counts per position. A derived table, not three correlated
# EXISTS subqueries: proposal_candidate holds ~54 rows against proposal_position's 43,636,
# so this scans the small side once instead of probing it once per position.
CANDIDATE_ROLLUP = """
  LEFT OUTER JOIN (
    SELECT proposal_position_id,
           COUNT(*) AS names,
           SUM(proposal_status_id = 2) AS confirmed,
           SUM(is_nominated = 'Y') AS nominated
    FROM proposal_candidate
    WHERE is_active = 'Y'
    GROUP BY proposal_position_id
  ) CAND ON CAND.proposal_position_id = PP.proposal_position_id
"""

# `proposal_position.is_active` ('Y'/'N') is being added to the schema — the ALTER is
# in flight as this is written. This clause has to be right on both sides of that change:
# it is empty until the column exists and starts filtering the moment it does, without a
# code change. Checked once per process and cached, exactly as
# ../../portal-frontend-code/Backend/main.py's pp_active() does it — the two dashboards
# must agree about which positions are live, or a deactivated position would vanish from
# Dashboard 1 and stay on Dashboard 2.
_PP_ACTIVE = None


def pp_active():
    from db import run

    global _PP_ACTIVE
    if _PP_ACTIVE is None:
        _PP_ACTIVE = (
            "AND PP.is_active = 'Y' "
            if run("SHOW COLUMNS FROM proposal_position LIKE 'is_active'")
            else ""
        )
    return _PP_ACTIVE


# The join every position query starts from. enrollment_id = 1 is the current enrollment,
# the same filter every proposal_consituency read in ../../portal-frontend-code applies.
POSITION_FROM = f"""
  FROM proposal_position PP
  JOIN proposal_consituency PCon
    ON PP.proposal_constituency_id = PCon.proposal_consituency_id
  JOIN proposal_election_type PET
    ON PCon.proposal_election_type_id = PET.proposal_election_type_id
  LEFT OUTER JOIN main_election_type MET
    ON PET.main_election_type_id = MET.main_election_type_id
  JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id
  JOIN user_address UA ON PCon.address_id = UA.user_address_id
  LEFT OUTER JOIN constituency_reservation CR
    ON PP.constituency_reservation_id = CR.constituency_reservation_id
  {CANDIDATE_ROLLUP}
"""

# The four counters every aggregate shares. CAST(... AS SIGNED) throughout because SUM() is
# DECIMAL in MySQL and would otherwise reach the browser as "12451.0" next to COUNT(*)'s
# plain integer.
COUNTERS = """
  COUNT(*) AS total_locations,
  CAST(SUM(PP.max_positions) AS SIGNED) AS max_positions,
  CAST(SUM(PP.max_proposals) AS SIGNED) AS max_proposals,
  CAST(SUM(COALESCE(CAND.names, 0)) AS SIGNED) AS proposed_names,
  CAST(SUM(COALESCE(CAND.names, 0) > 0) AS SIGNED) AS started,
  CAST(SUM(COALESCE(CAND.confirmed, 0) > 0) AS SIGNED) AS confirmed,
  CAST(SUM(COALESCE(CAND.nominated, 0) > 0) AS SIGNED) AS nominated
"""

# One row per (body, election type, post) — Dashboard 2's whole main table, and the six
# progress bars in its header, from one call. Bodies with main_election_type_id NULL (the
# inactive GMC Ward type) come back with type_name NULL rather than being dropped: an
# unclaimed election type is a data problem to show, not to hide.
POSITION_SUMMARY_SELECT = f"""
SELECT MET.main_election_type_id, MET.type_name AS main_election_type,
       PET.proposal_election_type_id, PET.election_type, PET.order_no AS election_type_order,
       PR.proposal_role_id, PR.role_name, PR.order_no AS role_order,
       {COUNTERS}
{POSITION_FROM}
  {{joins}}
  WHERE PCon.enrollment_id = 1
  {{scope}}
GROUP BY MET.main_election_type_id, MET.type_name,
         PET.proposal_election_type_id, PET.election_type, PET.order_no,
         PR.proposal_role_id, PR.role_name, PR.order_no
ORDER BY MET.main_election_type_id, PET.order_no, PR.order_no
"""

# Both geo queries need the attributed assembly as a real column to group and join on, so
# they resolve it through a join rather than repeating ASSEMBLY_EXPR in the GROUP BY.
GEO_JOIN = f"""
  JOIN constituency AC ON AC.constituency_id = ({ASSEMBLY_EXPR})
  LEFT OUTER JOIN constituency P ON AC.parliament_id = P.constituency_id
"""

# The same counters for ONE post, split by parliament constituency — the top half of
# Dashboard 2's geo table. Each row's counters add back up to that post's totals, which is
# what ASSEMBLY_EXPR's one-assembly rule buys.
GEO_BY_PARLIAMENT_SELECT = f"""
SELECT P.constituency_id AS parliament_id, P.name AS parliament_name,
       {COUNTERS}
{POSITION_FROM}
  {GEO_JOIN}
  WHERE PCon.enrollment_id = 1
  {{position}}
  {{scope}}
GROUP BY P.constituency_id, P.name
ORDER BY P.name
"""

# ... and by assembly, the bottom half. Optionally narrowed to one parliament.
GEO_BY_ASSEMBLY_SELECT = f"""
SELECT AC.constituency_id AS assembly_id, AC.name AS assembly_name,
       AC.parliament_id, P.name AS parliament_name, {COUNTERS}
{POSITION_FROM}
  {GEO_JOIN}
  WHERE PCon.enrollment_id = 1
  {{position}}
  {{scope}}
  {{parliament}}
GROUP BY AC.constituency_id, AC.name, AC.parliament_id, P.name
ORDER BY AC.name
"""

# Dashboard 2's reservation cards. NULL reservation_type is a position with no reservation
# configured and is reported as its own bucket rather than folded into GENERAL.
RESERVATION_SUMMARY_SELECT = f"""
SELECT CR.constituency_reservation_id, CR.reservation_type, CR.caste_category_id, CR.gender,
       {COUNTERS}
{POSITION_FROM}
  {{joins}}
  WHERE PCon.enrollment_id = 1
  {{position}}
  {{scope}}
GROUP BY CR.constituency_reservation_id, CR.reservation_type, CR.caste_category_id, CR.gender
ORDER BY CR.reservation_type
"""

# The per-location stage, as one expression so the SELECT list and the stage filter cannot
# drift apart.
STAGE_EXPR = """
  CASE WHEN COALESCE(CAND.nominated, 0) > 0 THEN 3
       WHEN COALESCE(CAND.confirmed, 0) > 0 THEN 2
       WHEN COALESCE(CAND.names, 0) > 0 THEN 1
       ELSE 0 END
"""

# One page of locations for one post. Three place names, three levels: a mandal row is its
# tehsil, a town-based body is its town, a district-level body (ZP) is its district —
# without the last branch a Zilla Parishath location renders with no place under it.
LOCATIONS_SELECT = f"""
SELECT PP.proposal_position_id,
       PP.max_positions, PP.max_proposals, PP.started_time,
       PCon.proposal_consituency_id AS proposal_constituency_id,
       LB.name AS local_body_name,
       CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name
            WHEN L.local_election_body_id IS NOT NULL THEN CONCAT(L.name, ' Town')
            WHEN D.district_id IS NOT NULL THEN CONCAT(D.district_name, ' District')
       END AS mandal_town_name,
       T.tehsil_id, L.local_election_body_id AS town_id, D.district_id,
       CR.reservation_type, CR.caste_category_id, CR.gender AS reservation_gender,
       AC.constituency_id AS assembly_id, AC.name AS assembly_name,
       P.constituency_id AS parliament_id, P.name AS parliament_name,
       CAST(COALESCE(CAND.names, 0) AS SIGNED) AS names,
       CAST(COALESCE(CAND.confirmed, 0) AS SIGNED) AS confirmed_names,
       CAST(COALESCE(CAND.nominated, 0) AS SIGNED) AS nominated_names,
       {STAGE_EXPR} AS stage
{POSITION_FROM}
  {GEO_JOIN}
  JOIN constituency LB ON PCon.constituency_id = LB.constituency_id
  LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id
  LEFT OUTER JOIN local_election_body L
    ON UA.local_election_body = L.local_election_body_id
  LEFT OUTER JOIN district D ON UA.district_id = D.district_id
  WHERE PCon.enrollment_id = 1
  {{position}}
  {{scope}}
  {{stage}}
  {{reservation}}
ORDER BY LB.name, PP.proposal_position_id
LIMIT %s OFFSET %s
"""

# The same filters, counted — so the caller can page without guessing the total.
LOCATIONS_COUNT_SELECT = f"""
SELECT COUNT(*) AS total
{POSITION_FROM}
  {GEO_JOIN}
  WHERE PCon.enrollment_id = 1
  {{position}}
  {{scope}}
  {{stage}}
  {{reservation}}
"""

# The names on a set of locations, with the profile fields Dashboard 2's comparison table
# lays side by side. Fetched for one page of locations at a time (proposal_position_id IN
# (...)) rather than per row, so the list costs two queries however long the page is.
#
# Same cadre joins ../../portal-frontend-code/Backend/main.py's
# getProposalCandidatesByProposalPositionId uses, so one cadre reads identically on both
# dashboards — plus occupation, education and party_member_since, which Dashboard 2's
# comparison table shows and Dashboard 1's card does not.
LOCATION_CANDIDATES_SELECT = """
SELECT PC.proposal_candidate_id, PC.proposal_position_id, PC.tdp_cadre_id,
       PC.proposal_status_id, PS.status_name AS proposal_status, PC.is_nominated,
       TC.membership_id, TC.first_name AS member_name, TC.last_name,
       TC.gender, TC.age, TC.date_of_birth, TC.mobile_no,
       TC.relative_name, TC.relative_type, TC.party_member_since,
       CC.category_name, CT.caste_name,
       O.occupation, EQ.qualification AS education,
       C.constituency_id AS cadre_constituency_id, C.name AS cadre_constituency_name,
       CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name
            WHEN L.local_election_body_id IS NOT NULL THEN CONCAT(L.name, ' Town')
       END AS cadre_mandal_town_name,
       PY.panchayat_name, V.voter_id_card_no,
       CASE WHEN TC.image IS NOT NULL
            THEN CONCAT('https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/', TC.image)
            ELSE '' END AS img_url
FROM proposal_candidate PC
LEFT OUTER JOIN proposal_status PS ON PC.proposal_status_id = PS.proposal_status_id
JOIN tdp_cadre TC ON PC.tdp_cadre_id = TC.tdp_cadre_id
LEFT OUTER JOIN user_address UA ON TC.address_id = UA.user_address_id
LEFT OUTER JOIN constituency C ON UA.constituency_id = C.constituency_id
LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id
LEFT OUTER JOIN local_election_body L
  ON UA.local_election_body = L.local_election_body_id
LEFT OUTER JOIN panchayat PY ON UA.panchayat_id = PY.panchayat_id
LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id
LEFT OUTER JOIN caste CT ON CS.caste_id = CT.caste_id
LEFT OUTER JOIN caste_category_group CCG
  ON CS.caste_category_group_id = CCG.caste_category_group_id
LEFT OUTER JOIN caste_category CC ON CCG.caste_category_id = CC.caste_category_id
LEFT OUTER JOIN occupation O ON TC.occupation_id = O.occupation_id
LEFT OUTER JOIN educational_qualifications EQ
  ON TC.education_id = EQ.educational_qualification_id
LEFT OUTER JOIN voter V ON TC.voter_id = V.voter_id
WHERE PC.is_active = 'Y' AND PC.proposal_position_id IN ({ids})
ORDER BY PC.proposal_position_id, PC.proposal_candidate_id
"""

# --- lookups ---------------------------------------------------------------
MAIN_ELECTION_TYPES_SELECT = (
    "SELECT main_election_type_id, type_name FROM main_election_type "
    "ORDER BY main_election_type_id"
)

ELECTION_TYPES_SELECT = (
    "SELECT PET.proposal_election_type_id, PET.election_type, PET.order_no, "
    "PET.is_active, PET.main_election_type_id, MET.type_name AS main_election_type "
    "FROM proposal_election_type PET "
    "LEFT OUTER JOIN main_election_type MET "
    "ON PET.main_election_type_id = MET.main_election_type_id "
    "ORDER BY PET.order_no"
)

ROLES_SELECT = (
    "SELECT proposal_role_id, role_name, order_no FROM proposal_role ORDER BY order_no"
)

STATUSES_SELECT = (
    "SELECT proposal_status_id, status_name FROM proposal_status "
    "ORDER BY proposal_status_id"
)

RESERVATIONS_SELECT = (
    "SELECT constituency_reservation_id, reservation_type, caste_category_id, gender "
    "FROM constituency_reservation ORDER BY constituency_reservation_id"
)

# The level-4 picklist. election_type_id = 1 is a parliament row.
PARLIAMENTS_SELECT = (
    "SELECT C.constituency_id, C.name AS constituency_name "
    "FROM constituency C "
    "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
    "WHERE ES.election_type_id = 1 AND C.state_id = %s AND C.deform_date IS NULL "
    "ORDER BY C.name"
)

# The level-5 picklist, optionally narrowed to the parliaments the caller named.
ASSEMBLIES_SELECT = (
    "SELECT C.constituency_id, C.name AS constituency_name, "
    "C.parliament_id, P.name AS parliament_name, C.district_id "
    "FROM constituency C "
    "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
    "LEFT OUTER JOIN constituency P ON C.parliament_id = P.constituency_id "
    "WHERE ES.election_type_id = 2 AND C.state_id = %s AND C.deform_date IS NULL "
    "{filter} "
    "ORDER BY C.name"
)
