# Backend/schemas.py — request bodies for the write endpoints.
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Admin console sign-in. Checked against LOGIN_USERNAME/LOGIN_PASSWORD in
    .env and nothing else — see routers/auth.py."""
    username: str
    password: str


class RoleUpdate(BaseModel):
    user_type_id: int


class ActiveUpdate(BaseModel):
    is_active: str  # 'Y' or 'N'


class LevelUpdateItem(BaseModel):
    user_level_id: int
    location_value: Optional[int] = None


class LevelUpdate(BaseModel):
    locations: List[LevelUpdateItem]


class MemberCreate(BaseModel):
    tdp_cadre_id: int
    user_type_id: int
    locations: List[LevelUpdateItem]  # at least one — a login can hold several active scopes at once
    component_ids: List[int] = []


class ComponentGrant(BaseModel):
    component_id: int


class MemberSave(BaseModel):
    """Detail screen "Save changes". Every field is optional — omitted (None)
    means "leave this alone", so the frontend sends only what it changed."""
    user_type_id: Optional[int] = None
    is_active: Optional[str] = None
    locations: Optional[List[LevelUpdateItem]] = None
    component_ids: Optional[List[int]] = None


class CadreCreate(BaseModel):
    first_name: str
    mobile_no: str
    age: Optional[int] = None
    gender: Optional[str] = None           # 'M' or 'F', optional
    otp: Optional[str] = None              # 6 digits; server generates one if omitted
    valid_till: Optional[datetime] = None  # admin-picked expiry; defaults to +10 min if omitted


class OtpRegenerate(BaseModel):
    otp: Optional[str] = None              # 6 digits; server generates one if omitted
    valid_till: Optional[datetime] = None  # admin-picked expiry; defaults to +10 min if omitted
