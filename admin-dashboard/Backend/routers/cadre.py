# Backend/routers/cadre.py — create-flow step 1: find a cadre by MID or mobile,
# or create a brand-new one.
#
# Route order matters: /api/cadre/by-mobile/{mobile} is declared BEFORE
# /api/cadre/{mid}/access-types, because both are three-segment paths and
# FastAPI matches in declaration order.
import random
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from config import CADRE_IMAGE_BASE, DEFAULT_CADRE_IMAGE, OTP_DEFAULT_VALID_MINUTES
from db import run, run_write_tx
from queries import ACCESS_TYPES_BY_MID_SELECT, CADRE_BY_MOBILE_SELECT
from schemas import CadreCreate, CadreUpdate

router = APIRouter(prefix="/api/cadre", tags=["cadre"])

# The row both single-record lookups return — same columns whether the key is
# membership_id or tdp_cadre_id, so callers get one shape to handle.
CADRE_SELECT = (
    f"SELECT tdp_cadre_id, membership_id, first_name, last_name, mobile_no, "
    f"gender, age, enrollment_year, constituency_id, "
    f"CONCAT('{CADRE_IMAGE_BASE}', image) AS image_url "
    f"FROM tdp_cadre WHERE {{key}} = %s AND is_deleted = 'N' LIMIT 1"
)

# What create_cadre stamps on every cadre this console creates: the
# enrollment_year column at 2014 (a product decision, not the current year) plus
# one tdp_cadre_enrollment_year row per cycle in ENROLLMENT_YEAR_IDS. Legacy
# rows predate that, so update_cadre backfills whichever of them is missing.
ENROLLMENT_YEAR = 2014
ENROLLMENT_YEAR_IDS = (6, 7)  # 2022-2024 and 2024-2026


@router.get("/{mid}")
def cadre(mid: str):
    row = run(CADRE_SELECT.format(key="membership_id"), (mid,), one=True)
    if not row:
        raise HTTPException(status_code=404, detail="no cadre for that MID")
    return row


@router.get("/by-mobile/{mobile}")
def cadre_by_mobile(mobile: str):
    return run(CADRE_BY_MOBILE_SELECT, (mobile,))


# Lookup by the internal PK. membership_id is str(tdp_cadre_id) for rows this
# console created, but legacy rows carry unrelated MIDs — so a cadre-id search
# needs its own key rather than reusing /{mid}.
@router.get("/by-cadre-id/{cadre_id}")
def cadre_by_cadre_id(cadre_id: int):
    row = run(CADRE_SELECT.format(key="tdp_cadre_id"), (cadre_id,), one=True)
    if not row:
        raise HTTPException(status_code=404, detail="no cadre for that cadre id")
    return row


# Update the writable half of an existing cadre. Granting access to someone who
# already has a tdp_cadre row goes through here first: name, membership_id,
# tdp_cadre_id and mobile_no are deliberately NOT accepted — they are the
# identity the admin searched on, and the console shows them read-only.
#
# The enrollment stamps are brought in line with what create_cadre would have
# written, but only where they are missing: an existing enrollment_year is left
# alone rather than overwritten to 2014, and a cycle row that is already there
# (is_deleted='N') is not duplicated. A row soft-deleted earlier is revived
# rather than re-inserted, so re-granting access can't stack duplicates.
@router.put("/{cadre_id}")
def update_cadre(cadre_id: int, body: CadreUpdate):
    gender = (body.gender or "").strip().upper() or None
    if gender and gender not in ("M", "F"):
        raise HTTPException(status_code=400, detail="gender must be 'M' or 'F'")
    if body.age is not None and not (0 < body.age < 150):
        raise HTTPException(status_code=400, detail="age must be a realistic value")

    def _update(cur):
        cur.execute(
            "SELECT tdp_cadre_id, enrollment_year FROM tdp_cadre "
            "WHERE tdp_cadre_id=%s AND is_deleted='N'",
            (cadre_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="no cadre for that cadre id")

        sets, args = [], []
        if body.age is not None:
            sets.append("age=%s")
            args.append(body.age)
        if gender:
            sets.append("gender=%s")
            args.append(gender)
        if not row["enrollment_year"]:
            sets.append("enrollment_year=%s")
            args.append(ENROLLMENT_YEAR)
        if sets:
            cur.execute(
                f"UPDATE tdp_cadre SET {', '.join(sets)}, update_time=NOW() WHERE tdp_cadre_id=%s",
                (*args, cadre_id),
            )

        for enrollment_year_id in ENROLLMENT_YEAR_IDS:
            cur.execute(
                "SELECT tdp_cadre_enrollment_year_id, is_deleted "
                "FROM tdp_cadre_enrollment_year WHERE tdp_cadre_id=%s AND enrollment_year_id=%s",
                (cadre_id, enrollment_year_id),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO tdp_cadre_enrollment_year "
                    "(tdp_cadre_id, enrollment_year_id, inserted_date, inserted_time, is_deleted) "
                    "VALUES (%s, %s, CURDATE(), NOW(), 'N')",
                    (cadre_id, enrollment_year_id),
                )
            elif existing["is_deleted"] != "N":
                cur.execute(
                    "UPDATE tdp_cadre_enrollment_year SET is_deleted='N', "
                    "updated_date=CURDATE(), updated_time=NOW() "
                    "WHERE tdp_cadre_enrollment_year_id=%s",
                    (existing["tdp_cadre_enrollment_year_id"],),
                )

        cur.execute(CADRE_SELECT.format(key="tdp_cadre_id"), (cadre_id,))
        return cur.fetchone()

    return run_write_tx(_update)


@router.get("/{mid}/access-types")
def cadre_access_types(mid: str):
    rows = run(ACCESS_TYPES_BY_MID_SELECT, (mid,))
    active = [r for r in rows if r["is_active"] == "Y"]
    return {
        "membership_id": mid,
        "total_grants": len(rows),
        "active_grants": len(active),
        "grants": rows,
    }


# Create a brand-new tdp_cadre record + its enrollment-year rows + its first
# login_otp_details row (Create Membership ID → manual path, no existing cadre
# matched).
#
# The MID is NOT admin-chosen: tdp_cadre_id is the auto-increment PK, so the
# row is inserted first (membership_id left NULL), then membership_id is set to
# str(tdp_cadre_id) in the same transaction — that guarantees uniqueness for
# free (no collision-retry needed, unlike a randomly-picked MID) and keeps the
# login key and the internal id in lockstep by design.
#
# Every such record also gets a placeholder image key (no photo upload in this
# console) and is_synced='Y' (this row didn't come from a field-sync device),
# plus a static enrollment_year=2014 (product decision — not derived from the
# current year). It's also enrolled in both the 2022-2024 and 2024-2026 cycles
# (enrollment_year_id 6 and 7) via two tdp_cadre_enrollment_year rows, per
# product decision — not just whichever cycle happens to be current.
#
# There is no SMS gateway wired into this backend — the generated OTP is
# returned directly in the response for the admin UI to display, not texted.
# `otp`/`valid_till` let the admin pick both instead of accepting the
# server-generated 6-digit code and default +OTP_DEFAULT_VALID_MINUTES expiry.
@router.post("", status_code=201)
def create_cadre(body: CadreCreate):
    first_name = body.first_name.strip()
    mobile = body.mobile_no.strip()
    if not first_name:
        raise HTTPException(status_code=400, detail="first_name is required")
    if not re.fullmatch(r"\d{10}", mobile):
        raise HTTPException(status_code=400, detail="mobile_no must be exactly 10 digits")

    gender = (body.gender or "").strip().upper() or None
    if gender and gender not in ("M", "F"):
        raise HTTPException(status_code=400, detail="gender must be 'M' or 'F'")
    if body.age is not None and not (0 < body.age < 150):
        raise HTTPException(status_code=400, detail="age must be a realistic value")

    requested_otp = (body.otp or "").strip()
    if requested_otp and not re.fullmatch(r"\d{6}", requested_otp):
        raise HTTPException(status_code=400, detail="otp must be exactly 6 digits")
    if body.valid_till and body.valid_till <= datetime.now():
        raise HTTPException(status_code=400, detail="valid_till must be in the future")

    def _create(cur):
        cur.execute(
            "INSERT INTO tdp_cadre (first_name, mobile_no, gender, age, image, "
            "enrollment_year, is_deleted, is_synced, data_source_type, inserted_time) "
            "VALUES (%s, %s, %s, %s, %s, 2014, 'N', 'Y', 'WEB', NOW())",
            (first_name, mobile, gender, body.age, DEFAULT_CADRE_IMAGE),
        )
        cadre_id = cur.lastrowid
        mid = str(cadre_id)
        cur.execute("UPDATE tdp_cadre SET membership_id=%s WHERE tdp_cadre_id=%s", (mid, cadre_id))

        # Enrollment-year membership: this console always enrolls a newly-created
        # cadre in both the 2022-2024 and 2024-2026 cycles (enrollment_year_id 6
        # and 7), one row each, rather than just the currently-active cycle.
        for enrollment_year_id in (6, 7):
            cur.execute(
                "INSERT INTO tdp_cadre_enrollment_year "
                "(tdp_cadre_id, enrollment_year_id, inserted_date, inserted_time, is_deleted) "
                "VALUES (%s, %s, CURDATE(), NOW(), 'N')",
                (cadre_id, enrollment_year_id),
            )

        # generated_time is a fixed '2026-12-31' for a brand-new MID, not NOW()
        # — product decision. The real admin-picked expiry still goes in
        # updated_time. Note this is the column the separate member-facing
        # login flow window-checks.
        otp = requested_otp or f"{random.randint(0, 999999):06d}"
        expires_at = body.valid_till or (datetime.now() + timedelta(minutes=OTP_DEFAULT_VALID_MINUTES))
        cur.execute(
            "UPDATE login_otp_details SET is_valid='N', updated_time=NOW() "
            "WHERE tdp_cadre_id=%s AND is_valid='Y'",
            (cadre_id,),
        )
        cur.execute(
            "INSERT INTO login_otp_details "
            "(tdp_cadre_id, membership_id, mobile_no, otp, generated_time, updated_time, is_valid) "
            "VALUES (%s, %s, %s, %s, '2026-12-31', %s, 'Y')",
            (cadre_id, mid, mobile, otp, expires_at),
        )
        return {
            "tdp_cadre_id": cadre_id, "membership_id": mid,
            "first_name": first_name, "mobile_no": mobile,
            "gender": gender, "age": body.age,
            "image_url": f"{CADRE_IMAGE_BASE}{DEFAULT_CADRE_IMAGE}",
            "otp": otp, "expires_at": expires_at,
        }

    return run_write_tx(_create)
