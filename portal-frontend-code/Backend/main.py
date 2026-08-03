import binascii
import datetime
import decimal
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def env(key):
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"{key} is not set. Copy .env.example to .env in the project root "
            f"and fill in the database credentials."
        )
    return value


DB = {
    "host": env("DB_HOST"),
    "port": int(env("DB_PORT")),
    "user": env("DB_USER"),
    "password": env("DB_PASSWORD"),
    "database": env("DB_NAME"),
    "cursorclass": pymysql.cursors.DictCursor,
}

# Cadre performance scores live in a database owned by the ratings pipeline, on its own
# server. It is optional: with any of host/user/password unset, RATINGS_DB stays None
# and S17 answers {"configured": false} instead of failing, so the wizard renders
# without scores rather than not at all.
RATINGS_DB = None
if all(os.environ.get(k) for k in ("REPORT_RATINGS_DB_HOST", "REPORT_RATINGS_DB_USER", "REPORT_RATINGS_DB_PASSWORD")):
    RATINGS_DB = {
        "host": os.environ["REPORT_RATINGS_DB_HOST"],
        "port": int(os.environ.get("REPORT_RATINGS_DB_PORT", "3306")),
        "user": os.environ["REPORT_RATINGS_DB_USER"],
        "password": os.environ["REPORT_RATINGS_DB_PASSWORD"],
        "database": os.environ.get("REPORT_RATINGS_DB_NAME", "report_ratings"),
        "cursorclass": pymysql.cursors.DictCursor,
    }

app = FastAPI(title="Local Body Elections API")

# CORSMiddleware is registered *after* guard_response, at the bottom of this file.
# add_middleware prepends and index 0 is the outermost wrapper, so registering it last
# is what puts it outside the guard — otherwise the guard's 401 short-circuits before
# CORS runs and a cross-origin caller sees an opaque failure instead of the 401 that
# api.js's checkUnauthorized depends on.


def query(sql, args=None):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def insert(sql, args=None):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@app.get("/S1getProposalElectionTypes")
def get_proposal_election_types():
    return query(
        "SELECT proposal_election_type_id, election_type "
        "FROM proposal_election_type WHERE is_active = 'Y' ORDER BY order_no"
    )


@app.get("/S2getAssemblyConstituenciesInAState")
def get_assembly_constituencies_in_a_state():
    return query(
        "SELECT C.constituency_id, C.name AS constituency_name "
        "FROM constituency C, election_scope ES "
        "WHERE C.election_scope_id = ES.election_scope_id AND "
        "ES.election_type_id = 2 AND C.state_id = 1 AND "
        "C.deform_date IS NULL ORDER BY C.name"
    )


@app.get("/S3getMandalsInAConstituency")
def get_mandals_in_a_constituency(constituency_id: int):
    return query(
        "SELECT T.tehsil_id, T.tehsil_name "
        "FROM tehsil_constituency TC, tehsil T "
        "WHERE TC.tehsil_id = T.tehsil_id AND TC.constituency_id = %s "
        "ORDER BY T.tehsil_name",
        (constituency_id,),
    )


@app.get("/S4getTownsInAConstituency")
def get_towns_in_a_constituency(constituency_id: int):
    return query(
        "SELECT L.local_election_body_id AS town_id, CONCAT(L.name, ' Town') AS town_name "
        "FROM assembly_local_election_body AL, local_election_body L "
        "WHERE AL.local_election_body_id = L.local_election_body_id AND "
        "AL.constituency_id = %s ORDER BY L.name",
        (constituency_id,),
    )


@app.get("/S5getProposalConstituenciesByTehsilId")
def get_proposal_constituencies_by_tehsil_id(
    constituency_id: int, tehsil_id: int, proposal_election_type_id: int
):
    return query(
        "SELECT PC.proposal_consituency_id, C.name AS constituency_name "
        "FROM proposal_consituency PC "
        "JOIN constituency C ON PC.constituency_id = C.constituency_id "
        "JOIN user_address UA ON PC.address_id = UA.user_address_id "
        "WHERE PC.proposal_election_type_id = %s AND "
        "UA.constituency_id = %s AND UA.tehsil_id = %s AND PC.enrollment_id = 1",
        (proposal_election_type_id, constituency_id, tehsil_id),
    )


@app.get("/S6getProposalConstituenciesByTownId")
def get_proposal_constituencies_by_town_id(
    constituency_id: int, town_id: int, proposal_election_type_id: int
):
    return query(
        "SELECT PC.proposal_consituency_id, C.name AS constituency_name "
        "FROM proposal_consituency PC "
        "JOIN constituency C ON PC.constituency_id = C.constituency_id "
        "JOIN user_address UA ON PC.address_id = UA.user_address_id "
        "WHERE PC.proposal_election_type_id = %s AND "
        "UA.constituency_id = %s AND UA.local_election_body = %s AND PC.enrollment_id = 1",
        (proposal_election_type_id, constituency_id, town_id),
    )


@app.get("/S7getProposalPositionsOverviewByProposalConstituencyId")
def get_proposal_positions_overview_by_proposal_constituency_id(
    proposal_constituency_id: int,
):
    return query(
        "SELECT PP.proposal_position_id, PR.proposal_role_id, PR.role_name, "
        "PP.max_positions, PP.max_proposals, "
        "COUNT(DISTINCT PC.tdp_cadre_id) AS proposed_cnt "
        "FROM proposal_position PP "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "LEFT OUTER JOIN proposal_candidate PC "
        "ON PP.proposal_position_id = PC.proposal_position_id AND PC.is_active = 'Y' "
        "WHERE PP.proposal_constituency_id = %s "
        "GROUP BY PP.proposal_position_id, PR.proposal_role_id, PR.role_name, "
        "PP.max_positions, PP.max_proposals, PR.order_no "
        "ORDER BY PR.order_no",
        (proposal_constituency_id,),
    )


@app.get("/S8getProposalPositionsByProposalConstituencyId")
def get_proposal_positions_by_proposal_constituency_id(proposal_constituency_id: int):
    return query(
        "SELECT PP.proposal_position_id, PR.role_name "
        "FROM proposal_position PP "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "WHERE PP.proposal_constituency_id = %s ORDER BY PR.order_no",
        (proposal_constituency_id,),
    )


@app.get("/S9getProposalConstituencyReservation")
def get_proposal_constituency_reservation(proposal_constituency_id: int):
    return query(
        "SELECT CR.constituency_reservation_id, CR.reservation_type "
        "FROM proposal_consituency PC "
        "JOIN constituency_reservation CR "
        "ON PC.constituency_reservation_id = CR.constituency_reservation_id "
        "WHERE PC.proposal_consituency_id = %s",
        (proposal_constituency_id,),
    )


@app.get("/S10checkProposalPositionAvailability")
def check_proposal_position_availability(proposal_position_id: int):
    return query(
        "SELECT CASE WHEN PP.max_proposals > COUNT(DISTINCT PC.tdp_cadre_id) "
        "THEN 'Available' ELSE 'Not Available' END AS availability "
        "FROM proposal_position PP "
        "LEFT OUTER JOIN proposal_candidate PC "
        "ON PP.proposal_position_id = PC.proposal_position_id AND PC.is_active = 'Y' "
        "WHERE PP.proposal_position_id = %s",
        (proposal_position_id,),
    )


# A proposal constituency's reservation. Eligibility is the reservation alone — a
# cadre's own address no longer has to match the constituency's chain (assembly ->
# mandal -> panchayat/town), so cadre from anywhere may be proposed.
def proposal_context(proposal_constituency_id):
    rows = query(
        "SELECT CR.reservation_type, CR.caste_category_id AS required_caste_category_id, "
        "CR.gender AS required_gender "
        "FROM proposal_consituency PC "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PC.constituency_reservation_id = CR.constituency_reservation_id "
        "WHERE PC.proposal_consituency_id = %s",
        (proposal_constituency_id,),
    )
    if not rows:
        raise HTTPException(404, "Unknown proposal_constituency_id")
    return rows[0]


# SELECT expression flagging whether a cadre satisfies the reservation. It is a flag
# rather than a WHERE clause so a search that matched only ineligible cadre can say so,
# instead of looking identical to a search that matched nobody. Requires TC and CCG in
# scope. A cadre with no caste category on record compares NULL, so falls to 'N'.
def eligibility_flag(ctx):
    conditions = []
    args = []
    if ctx["required_caste_category_id"] is not None:
        conditions.append("CCG.caste_category_id = %s")
        args.append(ctx["required_caste_category_id"])
    if ctx["required_gender"] == "F":
        conditions.append("TC.gender = 'F'")
    if not conditions:
        return "'Y' AS eligible", []
    return "CASE WHEN " + " AND ".join(conditions) + " THEN 'Y' ELSE 'N' END AS eligible", args


class AssignProposalCandidate(BaseModel):
    proposal_position_id: int
    tdp_cadre_id: int


@app.post("/S11assignProposalCandidate")
def assign_proposal_candidate(body: AssignProposalCandidate):
    position = query(
        "SELECT PP.max_proposals, CR.reservation_type, "
        "CR.caste_category_id AS required_caste_category_id, "
        "CR.gender AS required_gender "
        "FROM proposal_position PP "
        "JOIN proposal_consituency PCon "
        "ON PP.proposal_constituency_id = PCon.proposal_consituency_id "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PCon.constituency_reservation_id = CR.constituency_reservation_id "
        "WHERE PP.proposal_position_id = %s",
        (body.proposal_position_id,),
    )
    if not position:
        raise HTTPException(404, "Unknown proposal_position_id")
    position = position[0]

    cadre = query(
        "SELECT TC.gender, CCG.caste_category_id "
        "FROM tdp_cadre TC "
        "LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id "
        "LEFT OUTER JOIN caste_category_group CCG "
        "ON CS.caste_category_group_id = CCG.caste_category_group_id "
        "WHERE TC.tdp_cadre_id = %s",
        (body.tdp_cadre_id,),
    )
    if not cadre:
        raise HTTPException(404, "Unknown tdp_cadre_id")
    cadre = cadre[0]

    # required_caste_category_id / required_gender are NULL when the proposal
    # constituency has no reservation, which means anyone is eligible.
    if position["required_caste_category_id"] is not None:
        if cadre["caste_category_id"] is None:
            raise HTTPException(409, "Cadre has no caste category on record")
        if cadre["caste_category_id"] != position["required_caste_category_id"]:
            raise HTTPException(
                409, f"Position is reserved for {position['reservation_type']}"
            )
    if position["required_gender"] == "F" and cadre["gender"] != "F":
        raise HTTPException(
            409, f"Position is reserved for {position['reservation_type']}"
        )

    if check_proposal_position_availability(body.proposal_position_id)[0][
        "availability"
    ] != "Available":
        raise HTTPException(409, "Position has reached max_proposals")

    already = query(
        "SELECT proposal_candidate_id FROM proposal_candidate "
        "WHERE proposal_position_id = %s AND tdp_cadre_id = %s AND is_active = 'Y'",
        (body.proposal_position_id, body.tdp_cadre_id),
    )
    if already:
        raise HTTPException(409, "Cadre is already proposed for this position")

    proposal_candidate_id = insert(
        "INSERT INTO proposal_candidate "
        "(proposal_position_id, tdp_cadre_id, is_active, enrollment_id, inserted_time) "
        "VALUES (%s, %s, 'Y', 1, NOW())",
        (body.proposal_position_id, body.tdp_cadre_id),
    )
    return {"proposal_candidate_id": proposal_candidate_id}


CADRE_SEARCH_FILTERS = {
    "MembershipId": "TC.membership_id = %s",
    "MobileNo": "TC.mobile_no = %s",
    "Name": "TC.first_name LIKE %s",
}

@app.get("/S12cadreSearch")
def cadre_search(proposal_constituency_id: int, search_type: str, search_value: str):
    if search_type not in CADRE_SEARCH_FILTERS:
        raise HTTPException(
            400, "search_type must be one of MembershipId, MobileNo, Name"
        )
    value = f"%{search_value}%" if search_type == "Name" else search_value
    eligible_sql, eligible_args = eligibility_flag(
        proposal_context(proposal_constituency_id)
    )

    return query(
        "SELECT " + eligible_sql + ", "
        "TC.tdp_cadre_id, TC.membership_id, TC.first_name AS member_name, "
        "TC.gender, TC.age, TC.relative_name, TC.relative_type, TC.mobile_no, "
        "CC.category_name, CT.caste_name, C.constituency_id, C.name AS constituency_name, "
        "CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name "
        "ELSE CONCAT(L.name, ' Town') END AS mandal_town_name, "
        "P.panchayat_name, V.voter_id_card_no, "
        "CASE WHEN TC.image IS NOT NULL "
        "THEN CONCAT('https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/', TC.image) "
        "ELSE '' END AS img_url "
        "FROM tdp_cadre TC "
        "JOIN user_address UA ON TC.address_id = UA.user_address_id "
        "JOIN constituency C ON UA.constituency_id = C.constituency_id "
        "LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id "
        "LEFT OUTER JOIN local_election_body L ON UA.local_election_body = L.local_election_body_id "
        "LEFT OUTER JOIN panchayat P ON UA.panchayat_id = P.panchayat_id "
        "LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id "
        "LEFT OUTER JOIN caste CT ON CS.caste_id = CT.caste_id "
        "LEFT OUTER JOIN caste_category_group CCG "
        "ON CS.caste_category_group_id = CCG.caste_category_group_id "
        "LEFT OUTER JOIN caste_category CC ON CCG.caste_category_id = CC.caste_category_id "
        "LEFT OUTER JOIN voter V ON TC.voter_id = V.voter_id "
        "WHERE TC.is_deleted = 'N' AND " + CADRE_SEARCH_FILTERS[search_type],
        (*eligible_args, value),
    )


@app.get("/S13getProposalCandidatesByProposalPositionId")
def get_proposal_candidates_by_proposal_position_id(proposal_position_id: int):
    return query(
        "SELECT PC.proposal_candidate_id, TC.tdp_cadre_id, TC.membership_id, "
        "TC.first_name AS member_name, TC.gender, TC.age, TC.relative_name, "
        "TC.relative_type, TC.mobile_no, CC.category_name, CT.caste_name, "
        "C.constituency_id, C.name AS constituency_name, "
        "CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name "
        "ELSE CONCAT(L.name, ' Town') END AS mandal_town_name, "
        "P.panchayat_name, V.voter_id_card_no, "
        "CASE WHEN TC.image IS NOT NULL "
        "THEN CONCAT('https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/', TC.image) "
        "ELSE '' END AS img_url "
        "FROM proposal_candidate PC "
        "JOIN tdp_cadre TC ON PC.tdp_cadre_id = TC.tdp_cadre_id "
        "JOIN user_address UA ON TC.address_id = UA.user_address_id "
        "JOIN constituency C ON UA.constituency_id = C.constituency_id "
        "LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id "
        "LEFT OUTER JOIN local_election_body L ON UA.local_election_body = L.local_election_body_id "
        "LEFT OUTER JOIN panchayat P ON UA.panchayat_id = P.panchayat_id "
        "LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id "
        "LEFT OUTER JOIN caste CT ON CS.caste_id = CT.caste_id "
        "LEFT OUTER JOIN caste_category_group CCG "
        "ON CS.caste_category_group_id = CCG.caste_category_group_id "
        "LEFT OUTER JOIN caste_category CC ON CCG.caste_category_id = CC.caste_category_id "
        "LEFT OUTER JOIN voter V ON TC.voter_id = V.voter_id "
        "WHERE PC.proposal_position_id = %s AND PC.is_active = 'Y' "
        "ORDER BY PC.proposal_candidate_id",
        (proposal_position_id,),
    )


def ratings_query(sql, args=None):
    conn = pymysql.connect(**RATINGS_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


# The two rating procedures take one comma-separated list of membership ids and write
# their output to cadre_performace_report; the result sets they also emit are of no use
# here, but every one has to be drained before the connection can be reused.
def ratings_call(procedure, mids):
    conn = pymysql.connect(**RATINGS_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CALL {procedure}(%s)", (",".join(mids),))
            while cur.nextset():
                pass
        conn.commit()
    finally:
        conn.close()


def jsonable(row):
    """DictCursor hands back Decimal and date objects, neither of which json encodes."""
    out = {}
    for key, value in row.items():
        if isinstance(value, decimal.Decimal):
            out[key] = float(value)
        elif isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def normalize_mids(mids):
    """Digits only ('#1506 7518' -> '15067518'), blanks dropped, order and first
    occurrence kept. The wizard sends what S12 returned, but a pasted id may carry the
    '#' the rest of the party's tooling prints."""
    out = []
    for raw in mids:
        mid = "".join(ch for ch in str(raw) if ch.isdigit())
        if mid and mid not in out:
            out.append(mid)
    return out


def mid_key(mid):
    """Canonical key for matching one membership id across the two tables:
    cadre_performace_report stores it as varchar (so possibly zero-padded) while
    leader_feedback stores it as an INT. Leading zeros are what differ."""
    digits = "".join(ch for ch in str(mid) if ch.isdigit())
    return str(int(digits)) if digits else ""


def placeholders(values):
    return ", ".join(["%s"] * len(values))


# The 11 per-category POINTS columns that make up the performance half of the score.
# Column names are the report's own, spaces and all.
SCORE_POINT_COLUMNS = (
    "POINTS (Pedala Sevalo)",
    "POINTS (1st Membership)",
    "POINTS (No of Times)",
    "POINTS (Referrals)",
    "POINTS (Mandal Vote Share)",
    "POINTS (Booth Vote Share)",
    "MANDAL/TOWN 5% POINTS",
    "BOOTH 5% POINTS",
    "MANDAL/TOWN 15%",
    "BOOTH 15%",
    "POINTS (Positions)",
)

# leader_feedback holds one q<n>_option / q<n>_points pair per question id.
FEEDBACK_QUESTION_IDS = (16, 17, 18, 19, 20, 21, 24)

FEEDBACK_QUESTIONS = None


def feedback_questions():
    """Labels for the feedback rows, read once. They live in members_track, a different
    database on the same server. A failure here is cosmetic — the answers still render,
    keyed by question id — so it must not take the whole response down with it."""
    global FEEDBACK_QUESTIONS
    if FEEDBACK_QUESTIONS is None:
        ids = ", ".join(str(q) for q in FEEDBACK_QUESTION_IDS)
        try:
            rows = ratings_query(
                "SELECT question_id, question_name FROM members_track.question "
                f"WHERE question_id IN ({ids})"
            )
        except pymysql.Error:
            rows = []
        names = {row["question_id"]: row["question_name"] for row in rows}
        FEEDBACK_QUESTIONS = [
            {"question_id": q, "question_name": names.get(q)} for q in FEEDBACK_QUESTION_IDS
        ]
    return FEEDBACK_QUESTIONS


def performance_reports(mids):
    """{mid_key: report row}. The table's name really is spelt 'performace'."""
    rows = ratings_query(
        f"SELECT * FROM cadre_performace_report WHERE `MID` IN ({placeholders(mids)})",
        tuple(mids),
    )
    return {mid_key(row["MID"]): jsonable(row) for row in rows if mid_key(row["MID"])}


def leader_feedback(mids):
    """{mid_key: {"score": n, "answers": {question_id: {option, points}}}}."""
    columns = ["membership_id", "score"]
    for question in FEEDBACK_QUESTION_IDS:
        columns += [f"q{question}_option", f"q{question}_points"]
    rows = ratings_query(
        f"SELECT {', '.join(columns)} FROM leader_feedback "
        f"WHERE membership_id IN ({placeholders(mids)})",
        tuple(mids),
    )
    out = {}
    for row in rows:
        row = jsonable(row)
        key = mid_key(row["membership_id"])
        if not key:
            continue
        out[key] = {
            "score": row["score"],
            "answers": {
                str(question): {
                    "option": row[f"q{question}_option"],
                    "points": row[f"q{question}_points"],
                }
                for question in FEEDBACK_QUESTION_IDS
            },
        }
    return out


def total_score(performance, feedback):
    """Half the performance points plus half the leader-feedback points — the same Total
    Score the membership analytics platform ranks on. None, not 0, when neither source
    has anything, so a cadre with no ratings reads as "no score" rather than as the
    worst candidate in the list."""
    perf = [performance.get(column) for column in SCORE_POINT_COLUMNS] if performance else []
    answers = [answer["points"] for answer in feedback["answers"].values()] if feedback else []
    if not any(v is not None for v in perf) and not any(v is not None for v in answers):
        return None
    return sum(v for v in perf if v is not None) / 2 + sum(v for v in answers if v is not None) / 2


@app.get("/S17getCadreScores")
def get_cadre_scores(mids: str):
    """Performance score, its per-category breakdown and the leader feedback behind it,
    for one or more membership ids — one candidate card and the whole compare table are
    the same payload, so they are the same call.

    Lookup-first: a membership id whose report row already exists is served straight
    from the table, and the rating procedures (seconds per id) run only for the rest.
    """
    if RATINGS_DB is None:
        return {"configured": False, "questions": [], "candidates": []}

    wanted = normalize_mids(mids.split(","))
    if not wanted:
        raise HTTPException(400, "mids must be a comma-separated list of membership ids")

    reports = performance_reports(wanted)
    missing = [mid for mid in wanted if mid_key(mid) not in reports]
    if missing:
        ratings_call("cadre_performance_update", missing)
        ratings_call("cadre_performance_report", missing)
        reports.update(performance_reports(missing))

    feedback = leader_feedback(wanted)
    candidates = []
    for mid in wanted:
        performance = reports.get(mid_key(mid))
        answers = feedback.get(mid_key(mid))
        candidates.append(
            {
                "membership_id": mid,
                "total_score": total_score(performance, answers),
                "performance": performance,
                "feedback": answers,
            }
        )
    return {"configured": True, "questions": feedback_questions(), "candidates": candidates}


# `user`.Hash_Key is PBKDF2 over an MD5 digest of the credentials, as written by the
# Java portal that owns the table:
#   digest   = md5(md5(username) + md5(password))   -- lowercase hex, concatenated
#   Hash_Key = hex(PBKDF2-HMAC-SHA1(digest, salt, 1000 iterations, 64 bytes))
# Salt_Key is hex over the *ASCII* salt that side used (e.g. '[B@3da6a354', a Java
# byte[].toString()), so it has to be un-hexed before it goes into PBKDF2.
def password_hash(username, password, salt_key):
    digest = hashlib.md5(
        (
            hashlib.md5(username.encode()).hexdigest()
            + hashlib.md5(password.encode()).hexdigest()
        ).encode()
    ).hexdigest()
    return hashlib.pbkdf2_hmac(
        "sha1", digest.encode(), binascii.unhexlify(salt_key), 1000, 64
    ).hex()


# Hex of the ASCII string 'nonexistent-user', used only to burn the same PBKDF2 time
# for an unknown username as for a known one. Its value is irrelevant; it just has to
# be valid hex of a plausible length.
DUMMY_SALT_KEY = b"nonexistent-user".hex()


SESSION_COOKIE = "lbe_session"
SESSION_TTL = 8 * 60 * 60  # seconds

# token -> {"user": {...}, "expires": epoch}. In process memory, so sessions do not
# survive a backend restart (with --reload, that means every code edit). The trade is
# that logging in needs no schema change; move this to a table to outlive restarts.
SESSIONS = {}

# Browsers must never be able to read the session token from JavaScript, so it goes in
# an httpOnly cookie rather than localStorage. `secure` is opt-in because dev and the
# PM2 deployment both serve plain HTTP; set COOKIE_SECURE=true wherever there is TLS.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"


def current_user(request):
    token = request.cookies.get(SESSION_COOKIE)
    session = SESSIONS.get(token) if token else None
    if not session:
        return None
    if session["expires"] < time.time():
        # pop, not del: the async middleware and the sync /S15me handler run on
        # different threads and can both reach this line for the same token, and the
        # loser of that race would raise KeyError -> 500 instead of a clean 401.
        SESSIONS.pop(token, None)
        return None
    return session["user"]


# Everything except logging in requires a session: the cadre endpoints serve personal
# data (names, mobile numbers, voter ids) and S11 writes.
PUBLIC_PATHS = {"/S14login", "/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def guard_response(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
        response = await call_next(request)
    elif not current_user(request):
        response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
    else:
        response = await call_next(request)

    # Every response here is either personal data (cadre names, mobile numbers, voter
    # ids) or the login identity, and none of it is cacheable per-user: without this
    # the browser may keep it on disk past logout and hand it to whoever signs in next
    # on the same machine. It is also what makes each login re-fetch from the network.
    response.headers["Cache-Control"] = "no-store"
    return response


# Failed logins are throttled per username, not per client IP: dev and preview both
# proxy /api through Vite, so every request arrives from 127.0.0.1 and an IP bucket
# would throttle all users at once. The cost is that someone can lock a known username
# out for the window; the benefit is that guessing its password is capped at 10 tries.
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW = 15 * 60  # seconds
LOGIN_ATTEMPTS = {}


def recent_failures(username):
    now = time.time()
    hits = [t for t in LOGIN_ATTEMPTS.get(username, []) if now - t < LOGIN_WINDOW]
    if hits:
        LOGIN_ATTEMPTS[username] = hits
    else:
        LOGIN_ATTEMPTS.pop(username, None)
    return hits


# Neither dict is self-cleaning: current_user only drops a session when that exact
# token is presented again, and recent_failures only prunes the username it was asked
# about. So a user who logs in and never returns leaves a session behind forever, and
# an unauthenticated caller posting /S14login with a million distinct usernames grows
# LOGIN_ATTEMPTS without bound — the throttle cannot stop that, since it is keyed on
# the same username being invented. Sweep both whenever a login comes in.
# ponytail: O(n) scan on the login path; move to a background task if either dict
# ever gets big enough for that to show up in login latency.
def sweep_expired(now):
    for token in [t for t, s in SESSIONS.items() if s["expires"] < now]:
        SESSIONS.pop(token, None)
    for name in [
        n
        for n, hits in LOGIN_ATTEMPTS.items()
        if not hits or now - max(hits) >= LOGIN_WINDOW
    ]:
        LOGIN_ATTEMPTS.pop(name, None)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/S14login")
def login(body: LoginRequest, response: Response):
    sweep_expired(time.time())
    if len(recent_failures(body.username)) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")

    # username is indexed but not unique, so every row carrying the name is a
    # candidate; the hash decides which one (if any) the password belongs to.
    rows = query(
        "SELECT user_id, username, firstname, lastname, user_type, state_id, "
        "district_id, constituency_id, Hash_Key, Salt_Key FROM `user` "
        "WHERE username = %s AND Hash_Key IS NOT NULL AND Salt_Key IS NOT NULL",
        (body.username,),
    )
    if not rows:
        # An unknown username would otherwise skip the loop entirely and answer in a
        # fraction of the time a known one takes, which makes the 76k-row `user` table
        # enumerable despite the identical 401 below. Burn one PBKDF2 to match.
        password_hash(body.username, body.password, DUMMY_SALT_KEY)

    for row in rows:
        try:
            matched = hmac.compare_digest(
                password_hash(body.username, body.password, row["Salt_Key"]),
                row["Hash_Key"].lower(),
            )
        except (binascii.Error, TypeError, ValueError, AttributeError):
            # These rows are written by the Java portal, not by us: a Salt_Key that is
            # not valid even-length hex, or a Hash_Key stored as BINARY (so it arrives
            # as bytes and compare_digest rejects the mix), must not 500 the request —
            # that would abort before reaching the row whose password actually matches.
            # Skip the unusable row and keep going.
            continue
        if matched:
            user = {
                "user_id": row["user_id"],
                "username": row["username"],
                "firstname": row["firstname"],
                "lastname": row["lastname"],
                "user_type": row["user_type"],
                "state_id": row["state_id"],
                "district_id": row["district_id"],
                "constituency_id": row["constituency_id"],
            }
            LOGIN_ATTEMPTS.pop(body.username, None)
            # A fresh token per login, so a pre-set cookie cannot be fixated.
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {"user": user, "expires": time.time() + SESSION_TTL}
            response.set_cookie(
                SESSION_COOKIE,
                token,
                max_age=SESSION_TTL,
                httponly=True,
                samesite="lax",
                secure=COOKIE_SECURE,
                path="/",
            )
            return user

    LOGIN_ATTEMPTS.setdefault(body.username, []).append(time.time())
    # One message for both cases, so it does not reveal which usernames exist.
    raise HTTPException(401, "Invalid username or password")


@app.get("/S15me")
def me(request: Request):
    # guard_response has already rejected callers without a live session.
    return current_user(request)


@app.post("/S16logout")
def logout(request: Request, response: Response):
    SESSIONS.pop(request.cookies.get(SESSION_COOKIE), None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# Registered last on purpose, so it wraps guard_response rather than sitting inside it
# — see the note next to the FastAPI() call. allow_credentials is required for the
# session cookie to travel at all cross-origin; without it the allowlist above is
# decorative. Everything is same-origin through the Vite proxy today, so this only
# matters if the frontend is ever served from somewhere other than :9001.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:9001", "http://127.0.0.1:9001"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
