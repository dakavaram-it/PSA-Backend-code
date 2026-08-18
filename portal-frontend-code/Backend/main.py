import binascii
import datetime
import decimal
import hashlib
import hmac
import os
import queue
import time
from pathlib import Path
from uuid import uuid4

import boto3
import jwt
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
# and getCadreScores answers {"configured": false} instead of failing, so the wizard renders
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

# Nomination PDFs (uploadNominationFile) go to this bucket. Optional like RATINGS_DB: with the key pair
# unset, S3_CLIENT stays None and uploadNominationFile answers 503 rather than failing at import time, so
# the rest of the API still comes up without S3 configured.
S3_BUCKET = os.environ.get("S3_BUCKET", "leader-reports")
S3_CLIENT = None
if os.environ.get("S3_ACCESS_KEY") and os.environ.get("S3_SECRET_KEY"):
    S3_CLIENT = boto3.client(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )

app = FastAPI(title="Local Body Elections API")

# CORSMiddleware is registered *after* guard_response, at the bottom of this file.
# add_middleware prepends and index 0 is the outermost wrapper, so registering it last
# is what puts it outside the guard — otherwise the guard's 401 short-circuits before
# CORS runs and a cross-origin caller sees an opaque failure instead of the 401 that
# api.js's checkUnauthorized depends on.


# The database is an RDS cluster in us-east-1 and this service runs from India, so a
# fresh pymysql.connect() costs ~1.1s (TCP + MySQL auth handshake, several round trips)
# while the query it was opened for costs ~0.2s. Opening one per request — which is what
# this module used to do — made 84% of every call handshake, thrown away immediately
# after, and that was the delay between logging in and the first screen filling.
# Connections are pooled and reused instead, so only the first call on each pooled
# connection pays it. Every DB access goes through query/insert/update, so this is the
# only place that has to change.
#
# LifoQueue rather than FIFO: reusing the most recently returned connection keeps the
# pool naturally small under light load and lets the idle tail age out, instead of
# cycling every connection just often enough to keep it alive.
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
_POOL = queue.LifoQueue()
_RATINGS_POOL = queue.LifoQueue()


def _discard(conn):
    try:
        conn.close()
    except Exception:
        pass


def _release(pool, conn):
    if pool.qsize() >= POOL_MAX:
        _discard(conn)
    else:
        pool.put(conn)


def _checkout(config, pool):
    """A pooled connection if one is free, otherwise a new one. Returns (conn, is_new)
    so the caller can tell a stale pooled connection (worth retrying) from a brand-new
    one that failed for a real reason (not)."""
    try:
        return pool.get_nowait(), False
    except queue.Empty:
        return pymysql.connect(**config), True


def _read(config, pool, run):
    """Run a read on a pooled connection, retrying once on a fresh one.

    A connection idle past the server's wait_timeout is closed server-side without
    telling us, so the first statement on it raises. Retrying costs one wasted round
    trip in that case; pinging to check first would cost one on *every* call, which is
    the latency this pool exists to remove. Reads only — see _write()."""
    conn, is_new = _checkout(config, pool)
    try:
        result = run(conn)
    except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
        _discard(conn)
        if is_new:
            raise
        conn = pymysql.connect(**config)
        result = run(conn)
    _release(pool, conn)
    return result


def _write(config, pool, run):
    """Same, but verifies the connection with a ping before running rather than retrying
    after. A write that dies mid-flight may or may not have been applied server-side, so
    replaying it could double-insert; the ping's extra round trip is the safe trade, and
    writes are rare next to reads."""
    conn, is_new = _checkout(config, pool)
    if not is_new:
        try:
            conn.ping(reconnect=False)
        except Exception:
            _discard(conn)
            conn = pymysql.connect(**config)
    try:
        result = run(conn)
        conn.commit()
    except Exception:
        # Never return a connection that failed mid-statement to the pool: it may be
        # holding an open transaction or an undrained result set.
        _discard(conn)
        raise
    _release(pool, conn)
    return result


def query(sql, args=None):
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    return _read(DB, _POOL, run)


def insert(sql, args=None):
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid

    return _write(DB, _POOL, run)


def update(sql, args=None):
    def run(conn):
        with conn.cursor() as cur:
            return cur.execute(sql, args)

    return _write(DB, _POOL, run)


@app.get("/getProposalElectionTypes")
def get_proposal_election_types():
    return query(
        "SELECT proposal_election_type_id, election_type "
        "FROM proposal_election_type WHERE is_active = 'Y' ORDER BY order_no"
    )


@app.get("/getAssemblyConstituenciesInAState")
def get_assembly_constituencies_in_a_state():
    return query(
        "SELECT C.constituency_id, C.name AS constituency_name "
        "FROM constituency C, election_scope ES "
        "WHERE C.election_scope_id = ES.election_scope_id AND "
        "ES.election_type_id = 2 AND C.state_id = 1 AND "
        "C.deform_date IS NULL ORDER BY C.name"
    )


# The assemblies one user may work in — getAssemblyConstituenciesInAState narrowed to that user's grants. Three of them,
# unioned because a user may hold more than one: a row in user_state_access_info covers
# every assembly in the state, and a row in user_constituency_access_info is either a
# parliament (covering the assemblies whose parliament_id points at it) or one assembly
# itself. ES.election_type_id = 2 is what makes a constituency row an assembly rather than
# a parliament or a local body. No grants means no assemblies, not all of them.
def user_access_assemblies(user_id):
    return query(
        "SELECT C.constituency_id, C.name AS constituency_name "
        "FROM user_state_access_info SA "
        "JOIN constituency C ON C.state_id = SA.state_id "
        "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
        "WHERE SA.user_id = %s AND C.deform_date IS NULL AND ES.election_type_id = 2 "
        "UNION "
        "SELECT C.constituency_id, C.name AS constituency_name "
        "FROM user_constituency_access_info CA "
        "JOIN constituency C ON CA.constituency_id = C.parliament_id "
        "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
        "WHERE CA.user_id = %s AND C.deform_date IS NULL AND ES.election_type_id = 2 "
        "UNION "
        "SELECT C.constituency_id, C.name AS constituency_name "
        "FROM user_constituency_access_info CA "
        "JOIN constituency C ON CA.constituency_id = C.constituency_id "
        "JOIN election_scope ES ON C.election_scope_id = ES.election_scope_id "
        "WHERE CA.user_id = %s AND C.deform_date IS NULL AND ES.election_type_id = 2 "
        "ORDER BY constituency_name",
        (user_id, user_id, user_id),
    )


# The wizard's assembly picklist, replacing getAssemblyConstituenciesInAState. Like every write's user id, the one that
# scopes this comes from the session and never from a query parameter — a caller must not
# be able to ask for someone else's assemblies.
@app.get("/getUserAccessAssemblies")
def get_user_access_assemblies(request: Request):
    return user_access_assemblies(acting_user_id(request))


@app.get("/getMandalsInAConstituency")
def get_mandals_in_a_constituency(constituency_id: int):
    return query(
        "SELECT T.tehsil_id, T.tehsil_name "
        "FROM tehsil_constituency TC, tehsil T "
        "WHERE TC.tehsil_id = T.tehsil_id AND TC.constituency_id = %s "
        "ORDER BY T.tehsil_name",
        (constituency_id,),
    )


@app.get("/getTownsInAConstituency")
def get_towns_in_a_constituency(constituency_id: int):
    return query(
        "SELECT L.local_election_body_id AS town_id, CONCAT(L.name, ' Town') AS town_name "
        "FROM assembly_local_election_body AL, local_election_body L "
        "WHERE AL.local_election_body_id = L.local_election_body_id AND "
        "AL.constituency_id = %s ORDER BY L.name",
        (constituency_id,),
    )


@app.get("/getProposalConstituenciesByTehsilId")
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


@app.get("/getProposalConstituenciesByTownId")
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


@app.get("/getProposalPositionsOverviewByProposalConstituencyId")
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


@app.get("/getProposalPositionsByProposalConstituencyId")
def get_proposal_positions_by_proposal_constituency_id(proposal_constituency_id: int):
    return query(
        "SELECT PP.proposal_position_id, PR.role_name "
        "FROM proposal_position PP "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "WHERE PP.proposal_constituency_id = %s ORDER BY PR.order_no",
        (proposal_constituency_id,),
    )


@app.get("/getProposalConstituencyReservation")
def get_proposal_constituency_reservation(proposal_constituency_id: int):
    return query(
        "SELECT CR.constituency_reservation_id, CR.reservation_type "
        "FROM proposal_consituency PC "
        "JOIN constituency_reservation CR "
        "ON PC.constituency_reservation_id = CR.constituency_reservation_id "
        "WHERE PC.proposal_consituency_id = %s",
        (proposal_constituency_id,),
    )


@app.get("/checkProposalPositionAvailability")
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


# A proposal constituency's reservation, plus the assembly it sits in. The assembly is
# the only part of its address chain that scopes who may be proposed: a cadre has to be
# from the same assembly constituency, but not from the same mandal or panchayat.
# Reservation (caste category, gender) is the rest of eligibility.
def proposal_context(proposal_constituency_id):
    rows = query(
        "SELECT CR.reservation_type, CR.caste_category_id AS required_caste_category_id, "
        "CR.gender AS required_gender, UA.constituency_id AS assembly_constituency_id "
        "FROM proposal_consituency PC "
        "JOIN user_address UA ON PC.address_id = UA.user_address_id "
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


# proposal_status is a lookup table (1 Proposed, 2 Shortlisted, 3 Confirmed), so the id is
# checked against the table rather than against a list here — adding a status is a row, not
# a deploy. Proposed is the default, which is what every row written before the column
# existed means.
PROPOSED_STATUS_ID = 1


class AssignProposalCandidate(BaseModel):
    proposal_position_id: int
    tdp_cadre_id: int
    proposal_status_id: int = PROPOSED_STATUS_ID


# Who wrote a row comes from the session, never from the request body: the browser could
# put any user_id in a payload, and `proposal_candidate.inserted_user_id` /
# `updated_user_id` are an audit trail, so a caller must not be able to write someone
# else's id into one. guard_response has already rejected anyone without a live session by
# the time a handler runs, so this is never None outside PUBLIC_PATHS.
def acting_user_id(request):
    return current_user(request)["user_id"]


@app.post("/assignProposalCandidate")
def assign_proposal_candidate(body: AssignProposalCandidate, request: Request):
    position = query(
        "SELECT PP.max_proposals, CR.reservation_type, "
        "CR.caste_category_id AS required_caste_category_id, "
        "CR.gender AS required_gender, "
        "PUA.constituency_id AS assembly_constituency_id "
        "FROM proposal_position PP "
        "JOIN proposal_consituency PCon "
        "ON PP.proposal_constituency_id = PCon.proposal_consituency_id "
        "JOIN user_address PUA ON PCon.address_id = PUA.user_address_id "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PCon.constituency_reservation_id = CR.constituency_reservation_id "
        "WHERE PP.proposal_position_id = %s",
        (body.proposal_position_id,),
    )
    if not position:
        raise HTTPException(404, "Unknown proposal_position_id")
    position = position[0]

    if not query(
        "SELECT proposal_status_id FROM proposal_status WHERE proposal_status_id = %s",
        (body.proposal_status_id,),
    ):
        raise HTTPException(400, "Unknown proposal_status_id")

    cadre = query(
        "SELECT TC.gender, CCG.caste_category_id, "
        "UA.constituency_id AS assembly_constituency_id "
        "FROM tdp_cadre TC "
        "JOIN user_address UA ON TC.address_id = UA.user_address_id "
        "LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id "
        "LEFT OUTER JOIN caste_category_group CCG "
        "ON CS.caste_category_group_id = CCG.caste_category_group_id "
        "WHERE TC.tdp_cadre_id = %s",
        (body.tdp_cadre_id,),
    )
    if not cadre:
        raise HTTPException(404, "Unknown tdp_cadre_id")
    cadre = cadre[0]

    # The same assembly scope /cadreSearch filters on. Re-checked here because the search
    # filter is only what the browser was shown: this is the write, so it is where the
    # rule has to hold.
    if cadre["assembly_constituency_id"] != position["assembly_constituency_id"]:
        raise HTTPException(
            409, "Cadre belongs to a different assembly constituency"
        )

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
        "(proposal_position_id, tdp_cadre_id, proposal_status_id, is_active, "
        "enrollment_id, inserted_time, inserted_user_id) "
        "VALUES (%s, %s, %s, 'Y', 1, NOW(), %s)",
        (
            body.proposal_position_id,
            body.tdp_cadre_id,
            body.proposal_status_id,
            acting_user_id(request),
        ),
    )
    # First proposal against this position marks it started; later ones leave the
    # timestamp alone, so it reads as "first proposed", not "last proposed".
    update(
        "UPDATE proposal_position SET started_time = NOW() "
        "WHERE proposal_position_id = %s AND started_time IS NULL",
        (body.proposal_position_id,),
    )
    return {
        "proposal_candidate_id": proposal_candidate_id,
        "proposal_status_id": body.proposal_status_id,
    }


CADRE_SEARCH_FILTERS = {
    "MembershipId": "TC.membership_id = %s",
    "MobileNo": "TC.mobile_no = %s",
    "Name": "TC.first_name LIKE %s",
}

@app.get("/cadreSearch")
def cadre_search(proposal_constituency_id: int, search_type: str, search_value: str):
    if search_type not in CADRE_SEARCH_FILTERS:
        raise HTTPException(
            400, "search_type must be one of MembershipId, MobileNo, Name"
        )
    value = f"%{search_value}%" if search_type == "Name" else search_value
    ctx = proposal_context(proposal_constituency_id)
    eligible_sql, eligible_args = eligibility_flag(ctx)

    return query(
        "SELECT " + eligible_sql + ", "
        # Flagged, not filtered, for the same reason the reservation is: a cadre the
        # search matched in another assembly must read as "that id belongs to another
        # assembly", not as "no such id". Only rows with both flags 'Y' can be staged,
        # and assignProposalCandidate refuses the rest on write.
        "CASE WHEN UA.constituency_id = %s THEN 'Y' ELSE 'N' END AS in_assembly, "
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
        (*eligible_args, ctx["assembly_constituency_id"], value),
    )


@app.get("/getProposalCandidatesByProposalPositionId")
def get_proposal_candidates_by_proposal_position_id(proposal_position_id: int):
    return query(
        "SELECT PC.proposal_candidate_id, PC.proposal_status_id, "
        "PS.status_name AS proposal_status, TC.tdp_cadre_id, TC.membership_id, "
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
        # Outer join: rows written before proposal_status_id existed have it NULL, and
        # they are still assigned — dropping them would desync this list from getProposalPositionsOverviewByProposalConstituencyId's count.
        "LEFT OUTER JOIN proposal_status PS "
        "ON PC.proposal_status_id = PS.proposal_status_id "
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


class RemoveProposalCandidate(BaseModel):
    proposal_candidate_id: int


@app.post("/removeProposalCandidate")
def remove_proposal_candidate(body: RemoveProposalCandidate):
    """Drop a candidate from a position. `is_active` flips to 'N' rather than the row being
    deleted — that flag is what every read here filters on, so the candidate leaves getProposalCandidatesByProposalPositionId and
    getProposalPositionsOverviewByProposalConstituencyId's proposed_cnt and their slot reopens, while who was proposed and when survives."""
    removed = update(
        "UPDATE proposal_candidate SET is_active = 'N', updated_time = NOW() "
        "WHERE proposal_candidate_id = %s AND is_active = 'Y'",
        (body.proposal_candidate_id,),
    )
    if not removed:
        raise HTTPException(404, "Unknown or already removed proposal_candidate_id")
    return {"proposal_candidate_id": body.proposal_candidate_id}


class UpdateProposalCandidateStatus(BaseModel):
    proposal_candidate_id: int
    proposal_status_id: int


@app.post("/updateProposalCandidateStatus")
def update_proposal_candidate_status(body: UpdateProposalCandidateStatus, request: Request):
    """Move an already-assigned candidate between Proposed / Shortlisted / Confirmed.

    The only write here that changes a `proposal_candidate` row in place: assignProposalCandidate creates one
    and removeProposalCandidate deactivates one. It touches `proposal_status_id` alone, so the position, the
    cadre and `getProposalPositionsOverviewByProposalConstituencyId`'s `proposed_cnt` are unaffected — every status is a live row and
    consumes the same `max_proposals` slot, which is why no slot or eligibility re-check
    belongs here.

    `is_active = 'Y'` is part of the WHERE: a removed candidate is on no screen to
    restatus, and letting one through would edit a row getProposalCandidatesByProposalPositionId never returns.
    """
    if not query(
        "SELECT proposal_status_id FROM proposal_status WHERE proposal_status_id = %s",
        (body.proposal_status_id,),
    ):
        raise HTTPException(400, "Unknown proposal_status_id")

    changed = update(
        "UPDATE proposal_candidate SET proposal_status_id = %s, updated_time = NOW(), "
        "updated_user_id = %s WHERE proposal_candidate_id = %s AND is_active = 'Y'",
        (body.proposal_status_id, acting_user_id(request), body.proposal_candidate_id),
    )
    if not changed:
        # MySQL reports 0 affected rows for "no such row" and for "already that status"
        # alike, so check the row exists before calling it unknown — re-saving a status
        # that did not move is not an error the screen should show.
        if not query(
            "SELECT proposal_candidate_id FROM proposal_candidate "
            "WHERE proposal_candidate_id = %s AND is_active = 'Y'",
            (body.proposal_candidate_id,),
        ):
            raise HTTPException(404, "Unknown or removed proposal_candidate_id")

    return {
        "proposal_candidate_id": body.proposal_candidate_id,
        "proposal_status_id": body.proposal_status_id,
    }


@app.get("/getProposalPositionsWithCandidates")
def get_proposal_positions_with_candidates(request: Request):
    """Every proposal position that holds at least one active candidate, in the assemblies
    the session's user has access to — the Candidates screen's list, which is not reached
    by drilling down getProposalElectionTypes..getProposalConstituenciesByTownId and so cannot key off one proposal_constituency_id.

    The access filter is here rather than in the browser because it is access control: the
    screen's own four filters are cosmetic, but this one decides what leaves the server.
    It narrows the rows the Assembly dropdown is built from too, which is correct — an
    option nobody can open should not be offered.

    The join to proposal_candidate is inner on purpose: a position nobody was proposed
    for has nothing to show and must not appear. Both the local body (PCon.constituency_id,
    a panchayat/ward-level constituency row) and the assembly (through the address chain,
    the same way getProposalConstituenciesByTehsilId/getProposalConstituenciesByTownId resolve it) are returned, because the screen filters on the assembly
    while naming the local body.

    No query parameters: the caller filters this list in the browser, and the same rows are
    what populates its Role dropdown — a server-side role filter would narrow the very list
    the options are derived from.
    """
    access = [row["constituency_id"] for row in user_access_assemblies(acting_user_id(request))]
    if not access:
        return []
    return query(
        "SELECT PP.proposal_position_id, PP.max_positions, PP.max_proposals, "
        "PR.proposal_role_id, PR.role_name, "
        "PCon.proposal_consituency_id AS proposal_constituency_id, "
        "PET.proposal_election_type_id, PET.election_type, "
        "LB.name AS local_body_name, "
        "AC.constituency_id AS assembly_constituency_id, AC.name AS assembly_name, "
        "CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name "
        "ELSE CONCAT(L.name, ' Town') END AS mandal_town_name, "
        "CR.reservation_type, "
        "COUNT(DISTINCT PC.tdp_cadre_id) AS proposed_cnt, "
        # Rows written before proposal_status_id existed are proposals, which is what
        # COALESCE says here and what getProposalCandidatesByProposalPositionId and the card both read them back as.
        # CAST because SUM() is DECIMAL in MySQL, which would reach the browser as a float
        # ("2.0") next to proposed_cnt's plain integer.
        "CAST(SUM(CASE WHEN COALESCE(PC.proposal_status_id, %s) = 1 THEN 1 ELSE 0 END) AS UNSIGNED) AS proposed_status_cnt, "
        "CAST(SUM(CASE WHEN PC.proposal_status_id = 2 THEN 1 ELSE 0 END) AS UNSIGNED) AS shortlisted_status_cnt, "
        "CAST(SUM(CASE WHEN PC.proposal_status_id = 3 THEN 1 ELSE 0 END) AS UNSIGNED) AS conformed_status_cnt "
        "FROM proposal_position PP "
        "JOIN proposal_candidate PC "
        "ON PP.proposal_position_id = PC.proposal_position_id AND PC.is_active = 'Y' "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "JOIN proposal_consituency PCon "
        "ON PP.proposal_constituency_id = PCon.proposal_consituency_id "
        "JOIN proposal_election_type PET "
        "ON PCon.proposal_election_type_id = PET.proposal_election_type_id "
        "JOIN constituency LB ON PCon.constituency_id = LB.constituency_id "
        "JOIN user_address UA ON PCon.address_id = UA.user_address_id "
        "JOIN constituency AC ON UA.constituency_id = AC.constituency_id "
        "LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id "
        "LEFT OUTER JOIN local_election_body L "
        "ON UA.local_election_body = L.local_election_body_id "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PCon.constituency_reservation_id = CR.constituency_reservation_id "
        f"WHERE AC.constituency_id IN ({placeholders(access)}) "
        "GROUP BY PP.proposal_position_id, PP.max_positions, PP.max_proposals, "
        "PR.proposal_role_id, PR.role_name, PR.order_no, "
        "PCon.proposal_consituency_id, PET.proposal_election_type_id, PET.election_type, "
        "LB.name, AC.constituency_id, AC.name, T.tehsil_id, T.tehsil_name, L.name, "
        "CR.reservation_type "
        "ORDER BY AC.name, LB.name, PR.order_no",
        (PROPOSED_STATUS_ID, *access),
    )


@app.get("/getDashboardPositionsByConstituencyId")
def get_dashboard_positions_by_constituency_id(constituency_id: int):
    """Every proposal_position under one assembly, across every election type and every
    local body it resolves to (mandal or town) — the Dashboard screen's whole picture in
    one call. Without this, the same data would take an getProposalElectionTypes election types x getMandalsInAConstituency/getTownsInAConstituency
    mandals/towns x getProposalConstituenciesByTehsilId/getProposalConstituenciesByTownId x getProposalPositionsOverviewByProposalConstituencyId fan-out in the browser: one request per (election type,
    mandal-or-town) pair just to find which locations exist, then one more per location.

    LEFT OUTER JOIN to proposal_candidate, not INNER like getProposalPositionsWithCandidates: getProposalPositionsWithCandidates must hide a position
    nobody was proposed for, but this screen's "Not Started" is exactly that position.
    Unscoped by the caller's own access grants (unlike getProposalPositionsWithCandidates) — getProposalConstituenciesByTehsilId/getProposalConstituenciesByTownId already let any
    constituency_id through once the frontend has drilled down to it, and by the time the
    Dashboard calls this it has already picked the assembly off getUserAccessAssemblies's own list.

    Also carries `tehsil_id`/`town_id` (exactly one set per row, the other NULL) — getProposalConstituenciesByTehsilId/getProposalConstituenciesByTownId's
    own inputs — so a caller that already has one of these rows can jump the wizard
    straight to a location's Add Members search without re-deriving it through getMandalsInAConstituency/getTownsInAConstituency.

    Also carries `reservation_type`, off the same proposal_consituency.constituency_reservation_id
    getProposalConstituencyReservation reads, NULL when the local body has no reservation set — so the
    Dashboard's by-location table can show it without a second call per row.

    Also carries `started_time` off proposal_position itself — NULL means that role has not
    had a candidate proposed for it yet, non-NULL means it has (assignProposalCandidate stamps it on the
    first proposal). This is the Dashboard's own Proposal Status column, per role rather
    than per local body — a location holding more than one role rolls its own up from these.
    """
    return query(
        "SELECT PP.proposal_position_id, PP.max_positions, PP.max_proposals, PP.started_time, "
        "PR.proposal_role_id, PR.role_name, "
        "PCon.proposal_consituency_id AS proposal_constituency_id, "
        "LB.name AS local_body_name, "
        "PET.proposal_election_type_id, PET.election_type, "
        "CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name "
        "ELSE CONCAT(L.name, ' Town') END AS mandal_town_name, "
        "T.tehsil_id, L.local_election_body_id AS town_id, "
        "CR.reservation_type, "
        "COUNT(DISTINCT PC.tdp_cadre_id) AS proposed_cnt, "
        # Unlike getProposalPositionsWithCandidates, this does NOT default a missing proposal_status_id to Proposed:
        # getProposalPositionsWithCandidates's join to proposal_candidate is INNER so PC is never NULL there, but here
        # it's a LEFT JOIN (on purpose, so a position with no candidate still appears),
        # so PC.proposal_status_id is NULL both for "no candidate at all" and for a real
        # candidate whose status was never set. Counting only an explicit 1 handles both
        # correctly: no row, or no status, both read as not-yet-proposed here (0), never
        # coerced to 1.
        "CAST(SUM(CASE WHEN PC.proposal_status_id = 1 THEN 1 ELSE 0 END) AS UNSIGNED) AS proposed_status_cnt, "
        "CAST(SUM(CASE WHEN PC.proposal_status_id = 2 THEN 1 ELSE 0 END) AS UNSIGNED) AS shortlisted_status_cnt, "
        "CAST(SUM(CASE WHEN PC.proposal_status_id = 3 THEN 1 ELSE 0 END) AS UNSIGNED) AS conformed_status_cnt "
        "FROM proposal_consituency PCon "
        "JOIN user_address UA ON PCon.address_id = UA.user_address_id "
        "JOIN proposal_election_type PET ON PCon.proposal_election_type_id = PET.proposal_election_type_id "
        "JOIN constituency LB ON PCon.constituency_id = LB.constituency_id "
        "LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id "
        "LEFT OUTER JOIN local_election_body L ON UA.local_election_body = L.local_election_body_id "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PCon.constituency_reservation_id = CR.constituency_reservation_id "
        "JOIN proposal_position PP ON PP.proposal_constituency_id = PCon.proposal_consituency_id "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "LEFT OUTER JOIN proposal_candidate PC "
        "ON PP.proposal_position_id = PC.proposal_position_id AND PC.is_active = 'Y' "
        "WHERE UA.constituency_id = %s AND PCon.enrollment_id = 1 "
        "GROUP BY PP.proposal_position_id, PP.max_positions, PP.max_proposals, PP.started_time, "
        "PR.proposal_role_id, PR.role_name, PR.order_no, "
        "PCon.proposal_consituency_id, LB.name, "
        "PET.proposal_election_type_id, PET.election_type, T.tehsil_id, T.tehsil_name, "
        "L.name, L.local_election_body_id, CR.reservation_type "
        "ORDER BY PET.election_type, LB.name, PR.order_no",
        (constituency_id,),
    )


@app.get("/getDashboardCandidatesByStatus")
def get_dashboard_candidates_by_status(
    constituency_id: int, proposal_election_type_id: int, proposal_status_id: int
):
    """The candidate list behind one Dashboard stat tile (Proposed or Confirmed): every
    active proposal_candidate at proposal_status_id, under one assembly and election
    type — the same (assembly, election type) scope getDashboardPositionsByConstituencyId's tiles are already summed over,
    drilled down to the rows themselves rather than the count.

    `nomination_file_path` is a correlated subquery over election_candidate /
    election_candidate_file (file_type='Pdf', is_deleted not 'Y') rather than a JOIN: a
    candidate has at most one live election_candidate row in this flow, but a JOIN would
    still multiply the candidate row if that ever stopped being true, which would desync
    this list's count from getDashboardPositionsByConstituencyId's. NULL means no PDF has been uploaded for this candidate
    yet, which the caller reads as "nomination in progress" rather than "not confirmed".
    `is_deleted` is checked as "not 'Y'" rather than "= 'N'" because both tables leave it
    NULL on insert rather than defaulting it, and NULL = 'N' is NULL (never true) in SQL —
    that comparison would hide every row nobody has explicitly soft-deleted.

    Also carries `gender`, `category_name`/`caste_name` (the same caste join cadreSearch uses) and
    `reservation_type` for the position's own local body — so this drill-down table can show
    them without a second call per row.
    """
    return query(
        "SELECT PC.proposal_candidate_id, PC.tdp_cadre_id, TC.membership_id, "
        "TC.first_name AS member_name, TC.mobile_no, TC.gender, PR.role_name, "
        "CC.category_name, CT.caste_name, CR.reservation_type, "
        "LB.name AS local_body_name, "
        "CASE WHEN T.tehsil_id IS NOT NULL THEN T.tehsil_name "
        "ELSE CONCAT(L.name, ' Town') END AS mandal_town_name, "
        # The same S3 URL cadreSearch/getProposalCandidatesByProposalPositionId build, so one cadre's photo is the same everywhere.
        # '' rather than NULL when they have no image — the caller falls back to initials.
        "CASE WHEN TC.image IS NOT NULL "
        "THEN CONCAT('https://imagesearch-projectkv.s3.amazonaws.com/cadre_images/', TC.image) "
        "ELSE '' END AS img_url, "
        "(SELECT ECF.file_path FROM election_candidate EC "
        "JOIN election_candidate_file ECF ON ECF.election_candidate_id = EC.election_candidate_id "
        "WHERE EC.proposal_candidate_id = PC.proposal_candidate_id "
        "AND (EC.is_deleted IS NULL OR EC.is_deleted != 'Y') "
        "AND ECF.file_type = 'Pdf' AND (ECF.is_deleted IS NULL OR ECF.is_deleted != 'Y') "
        "ORDER BY ECF.election_candidate_file_id DESC LIMIT 1) AS nomination_file_path "
        "FROM proposal_candidate PC "
        "JOIN proposal_position PP ON PC.proposal_position_id = PP.proposal_position_id "
        "JOIN proposal_role PR ON PP.proposal_role_id = PR.proposal_role_id "
        "JOIN proposal_consituency PCon ON PP.proposal_constituency_id = PCon.proposal_consituency_id "
        "JOIN constituency LB ON PCon.constituency_id = LB.constituency_id "
        "JOIN user_address UA ON PCon.address_id = UA.user_address_id "
        "LEFT OUTER JOIN tehsil T ON UA.tehsil_id = T.tehsil_id "
        "LEFT OUTER JOIN local_election_body L ON UA.local_election_body = L.local_election_body_id "
        "LEFT OUTER JOIN constituency_reservation CR "
        "ON PCon.constituency_reservation_id = CR.constituency_reservation_id "
        "JOIN tdp_cadre TC ON PC.tdp_cadre_id = TC.tdp_cadre_id "
        "LEFT OUTER JOIN caste_state CS ON TC.caste_state_id = CS.caste_state_id "
        "LEFT OUTER JOIN caste CT ON CS.caste_id = CT.caste_id "
        "LEFT OUTER JOIN caste_category_group CCG ON CS.caste_category_group_id = CCG.caste_category_group_id "
        "LEFT OUTER JOIN caste_category CC ON CCG.caste_category_id = CC.caste_category_id "
        "WHERE UA.constituency_id = %s AND PCon.proposal_election_type_id = %s "
        "AND PC.proposal_status_id = %s AND PC.is_active = 'Y' AND PCon.enrollment_id = 1 "
        "ORDER BY LB.name, PR.order_no, TC.first_name",
        (constituency_id, proposal_election_type_id, proposal_status_id),
    )


@app.post("/uploadNominationFile")
async def upload_nomination_file(
    request: Request,
    proposal_candidate_id: int = Form(...),
    file: UploadFile = File(...),
):
    """The Confirmed-candidate upload button behind getDashboardCandidatesByStatus's nomination column: stores the
    PDF at leader-reports/election_nominations/DDMMYY/<uuid>.pdf and records it in
    election_candidate / election_candidate_file, which is what getDashboardCandidatesByStatus's correlated
    subquery reads back as `nomination_file_path`.

    election_candidate is reused across re-uploads (looked up by proposal_candidate_id)
    rather than inserted every time, since it is the row that names *who* the file
    belongs to; election_candidate_file gets a fresh row per upload instead, so a
    re-upload does not lose the previous file's record — getDashboardCandidatesByStatus already takes the latest by
    id, so an old row being left behind changes nothing it reads.
    """
    if S3_CLIENT is None:
        raise HTTPException(503, "File storage is not configured on the server.")
    if file.content_type != "application/pdf":
        raise HTTPException(400, "Only PDF files are accepted.")

    candidate = query(
        "SELECT proposal_position_id, tdp_cadre_id FROM proposal_candidate "
        "WHERE proposal_candidate_id = %s AND is_active = 'Y'",
        (proposal_candidate_id,),
    )
    if not candidate:
        raise HTTPException(404, "Unknown or removed proposal_candidate_id")
    candidate = candidate[0]

    content = await file.read()
    key = f"election_nominations/{datetime.datetime.now().strftime('%d%m%y')}/{uuid4()}.pdf"
    try:
        S3_CLIENT.put_object(Bucket=S3_BUCKET, Key=key, Body=content, ContentType="application/pdf")
    except (BotoCoreError, ClientError) as err:
        raise HTTPException(502, "Could not upload the file to storage.") from err
    file_path = f"{S3_BUCKET}/{key}"

    user_id = acting_user_id(request)
    existing = query(
        "SELECT election_candidate_id FROM election_candidate "
        "WHERE proposal_candidate_id = %s AND (is_deleted IS NULL OR is_deleted != 'Y')",
        (proposal_candidate_id,),
    )
    if existing:
        election_candidate_id = existing[0]["election_candidate_id"]
    else:
        election_candidate_id = insert(
            "INSERT INTO election_candidate "
            "(proposal_candidate_id, proposal_position_id, tdp_cadre_id, is_deleted, "
            "inserted_time, inserted_user_id) VALUES (%s, %s, %s, 'N', NOW(), %s)",
            (proposal_candidate_id, candidate["proposal_position_id"], candidate["tdp_cadre_id"], user_id),
        )

    insert(
        "INSERT INTO election_candidate_file "
        "(election_candidate_id, file_type, file_path, is_deleted, inserted_time, inserted_user_id) "
        "VALUES (%s, 'Pdf', %s, 'N', NOW(), %s)",
        (election_candidate_id, file_path, user_id),
    )

    return {"proposal_candidate_id": proposal_candidate_id, "file_path": file_path}


@app.get("/getNominationFileUrl")
def get_nomination_file_url(proposal_candidate_id: int):
    """A short-lived link to a candidate's uploaded nomination PDF, for the view icon
    next to getDashboardCandidatesByStatus's nomination badge. leader-reports blocks all public access (checked via
    get_public_access_block before this was built), so the file_path stored on uploadNominationFile's
    write is not itself a fetchable URL — the browser needs a presigned one, generated
    fresh per click rather than stored, so a link seen once cannot be replayed forever.
    """
    row = query(
        "SELECT ECF.file_path FROM election_candidate EC "
        "JOIN election_candidate_file ECF ON ECF.election_candidate_id = EC.election_candidate_id "
        "WHERE EC.proposal_candidate_id = %s AND (EC.is_deleted IS NULL OR EC.is_deleted != 'Y') "
        "AND ECF.file_type = 'Pdf' AND (ECF.is_deleted IS NULL OR ECF.is_deleted != 'Y') "
        "ORDER BY ECF.election_candidate_file_id DESC LIMIT 1",
        (proposal_candidate_id,),
    )
    if not row:
        raise HTTPException(404, "No nomination file on record for this candidate")
    if S3_CLIENT is None:
        raise HTTPException(503, "File storage is not configured on the server.")

    bucket, key = row[0]["file_path"].split("/", 1)
    url = S3_CLIENT.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    return {"url": url}


def ratings_query(sql, args=None):
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    return _read(RATINGS_DB, _RATINGS_POOL, run)


# The two rating procedures take one comma-separated list of membership ids and write
# their output to cadre_performace_report; the result sets they also emit are of no use
# here, but every one has to be drained before the connection can be reused. Draining is
# what makes the connection safe to hand back to the pool at all.
def ratings_call(procedure, mids):
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(f"CALL {procedure}(%s)", (",".join(mids),))
            while cur.nextset():
                pass

    _write(RATINGS_DB, _RATINGS_POOL, run)


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
    occurrence kept. The wizard sends what cadreSearch returned, but a pasted id may carry the
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


@app.get("/getCadreScores")
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


SESSION_TTL = 8 * 60 * 60  # seconds

# The session is a signed JWT and nothing else: no server-side session store, no cookie.
# The user row travels inside the token, so any worker of any restarted process can verify
# a request on its own. The trade is that a token cannot be revoked before it expires —
# /logout only tells the client to drop it. Shorten SESSION_TTL, or add a denylist, if that
# stops being acceptable.
JWT_SECRET = env("JWT_SECRET")
JWT_ALGORITHM = env("ALGORITHM")

# JWT bookkeeping, as opposed to the claims that are the identity. Anything here is
# stripped back off before a decoded token is handed to a caller.
JWT_META_CLAIMS = ("exp",)


def issue_token(user):
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=SESSION_TTL
    )
    return jwt.encode({**user, "exp": expires}, JWT_SECRET, algorithm=JWT_ALGORITHM)


# One transport: `Authorization: Bearer <token>`. Same for the browser, another origin,
# a mobile client or a script.
def bearer_token(request):
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def current_user(request):
    token = bearer_token(request)
    if not token:
        return None
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        # Expired, tampered with, signed by another key, or not a JWT at all: every one
        # of those is simply "no session" to every caller here.
        return None
    return {k: v for k, v in claims.items() if k not in JWT_META_CLAIMS}


# Everything except logging in requires a session: the cadre endpoints serve personal
# data (names, mobile numbers, voter ids) and assignProposalCandidate writes.
PUBLIC_PATHS = {"/login", "/docs", "/redoc", "/openapi.json"}


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


# recent_failures only prunes the username it was asked about, so an unauthenticated
# caller posting /login with a million distinct usernames grows LOGIN_ATTEMPTS without
# bound — the throttle cannot stop that, since it is keyed on the same username being
# invented. Sweep whenever a login comes in.
# ponytail: O(n) scan on the login path; move to a background task if the dict ever gets
# big enough for that to show up in login latency.
def sweep_expired(now):
    for name in [
        n
        for n, hits in LOGIN_ATTEMPTS.items()
        if not hits or now - max(hits) >= LOGIN_WINDOW
    ]:
        LOGIN_ATTEMPTS.pop(name, None)


class LoginRequest(BaseModel):
    username: str
    password: str


# What this user is allowed to see, via the groups they belong to. GROUP BY collapses
# the same entitlement reached through two groups; the row shape is one name per row.
# ponytail: fine while the list is a handful of names — if it ever runs to hundreds,
# keep the token thin and read them per request instead.
def entitlements_for(user_id):
    rows = query(
        "SELECT E.entitlement_type AS entitlement_name "
        "FROM user_group_relation UGR "
        "JOIN user_group_entitlement UGE ON UGR.user_group_id = UGE.user_group_id "
        "JOIN group_entitlement_relation GER "
        "ON UGE.group_entitlement_id = GER.group_entitlement_id "
        "JOIN entitlement E ON GER.entitlement_id = E.entitlement_id "
        "WHERE UGR.user_id = %s "
        "GROUP BY E.entitlement_id ORDER BY entitlement_name",
        (user_id,),
    )
    return [r["entitlement_name"] for r in rows]


@app.post("/login")
def login(body: LoginRequest):
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
                # Part of the identity, not a field beside it: it rides the JWT, so /me
                # hands it back on a reload and the server is what signed it — a client
                # cannot grant itself an entitlement it was not issued. The cost is that
                # a grant changed in the DB is not seen until the token expires
                # (SESSION_TTL) or the user logs in again.
                "entitlements": entitlements_for(row["user_id"]),
            }
            LOGIN_ATTEMPTS.pop(body.username, None)
            # The token is the whole session: the caller stores it and presents it as
            # `Authorization: Bearer <token>` on every later call.
            return {**user, "token": issue_token(user)}

    LOGIN_ATTEMPTS.setdefault(body.username, []).append(time.time())
    # One message for both cases, so it does not reveal which usernames exist.
    raise HTTPException(401, "Invalid username or password")


@app.get("/me")
def me(request: Request):
    # guard_response has already rejected callers without a live session.
    return current_user(request)


@app.post("/logout")
def logout():
    # Nothing to drop: a JWT is not stored server-side, so logging out is the client
    # discarding its token. The token itself stays valid until `exp` — that is the cost
    # of the stateless session, and why SESSION_TTL is hours rather than days.
    return {"ok": True}


# Registered last on purpose, so it wraps guard_response rather than sitting inside it
# — see the note next to the FastAPI() call. The session travels in a header, not a
# cookie, so what the allowlist gates is which origins may read a response at all.
# Everything is same-origin through the Vite proxy today, so this only matters if the
# frontend is ever served from somewhere other than :9001.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:9001", "http://127.0.0.1:9001"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
