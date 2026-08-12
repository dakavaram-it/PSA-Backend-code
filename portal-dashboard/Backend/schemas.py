# Backend/schemas.py — request bodies (Pydantic), mirrors
# ../admin-dashboard/Backend/schemas.py.
from typing import List, Optional

from pydantic import BaseModel


class UserSave(BaseModel):
    # access_type/access_value are only ever meaningful together — see the
    # pairing check in routers/users.py.
    access_type: Optional[str] = None
    access_value: Optional[str] = None
    is_enabled: Optional[str] = None  # 'Y' or 'N'
    # Password reset — validated in routers/users.py, but not yet applied;
    # see the 501 guard there and services.apply_password.
    password: Optional[str] = None


# Create New User (Portal Dashboard) — one write, no draft/staging the way the
# admin Create screen needs (there is no separate identity/access table pair
# behind a `user` row). access_type/access_value are required, not optional,
# unlike UserSave's PUT — a freshly created account is expected to carry a
# scope from the start.
class UserCreate(BaseModel):
    username: str
    password: str
    firstname: str
    lastname: str
    gender: str
    dateofbirth: str  # 'YYYY-MM-DD'
    mobile: str
    address: str
    access_type: str
    access_value: str
    is_otp_required: str  # 'Y' or 'N' — admin-picked, not defaulted


# Entitlement Management's Create Entitlement — the `entitlement` table's one
# write. entitlement_type is the only column besides the auto-increment PK.
class EntitlementCreate(BaseModel):
    entitlement_type: str


# Entitlement Management's Create Entitlement Group — one group_entitlement
# row (description) plus one group_entitlement_relation row per selected
# entitlement_id, from the "view entitlements" checklist.
class GroupEntitlementCreate(BaseModel):
    description: str
    entitlement_ids: List[int]


# Entitlement Management's Create User Group — one user_groups row (notes)
# plus one user_group_entitlement row per selected group_entitlement_id, from
# the "view entitlement groups" checklist. One level up from
# GroupEntitlementCreate: a user_group bundles entitlement groups, not raw
# entitlements — see services.insert_user_group.
class UserGroupCreate(BaseModel):
    notes: str
    group_entitlement_ids: List[int]


# Entitlement Management's Assign User to User Groups — one
# user_group_relation row per selected user_group_id, for the one user_id in
# the path. See services.assign_user_groups.
class UserGroupsAssign(BaseModel):
    group_ids: List[int]


# Entitlement Management's Assign entitlement groups to a user group (and its
# reverse, assign user groups to an entitlement group, which loops this same
# body one group_entitlement_id at a time) — one user_group_entitlement row
# per selected group_entitlement_id, for the one user_group_id in the path.
# See services.assign_group_entitlements.
class GroupEntitlementsAssign(BaseModel):
    group_entitlement_ids: List[int]


# Create Entitlement's "add to an existing entitlement group" option — one
# group_entitlement_relation row per selected entitlement_id (always a
# single-item list from that card), for the one group_entitlement_id in the
# path. See services.assign_entitlements_to_group.
class EntitlementsAssign(BaseModel):
    entitlement_ids: List[int]
