from typing import Any, List, Optional
from pydantic import BaseModel

class ApiResponse(BaseModel):
    success: bool = True
    message: str = "OK"
    data: Any = None

class ProposalCreateRequest(BaseModel):
    enrollmentId: int = 2
    boardLevelId: int
    locationValue: int
    locationName: Optional[str] = None
    departmentId: int
    boardId: int
    positionId: int
    nominatedPostMemberId: int
    nominatedPostPositionId: Optional[int] = None
    remarks: Optional[str] = None
    createdBy: int
    createdByName: Optional[str] = None

class ProposalCandidateAddItem(BaseModel):
    tdpCadreId: Optional[int] = None
    nominationPostCandidateId: Optional[int] = None
    nominatedPostId: Optional[int] = None
    nominatedPostApplicationId: Optional[int] = None
    sourceType: str = "CADRE_SEARCH"
    remarks: Optional[str] = None

class ProposalCandidateAddRequest(BaseModel):
    createdBy: int
    createdByName: Optional[str] = None
    candidates: List[ProposalCandidateAddItem]


class ManualCandidateCreateRequest(BaseModel):
    """Create a brand-new candidate directly on a proposal (not in tdp_cadre).

    Stored as a snapshot with tdp_cadre_id = NULL and source_type = 'MANUAL'.
    Geo (parliament/assembly/mandal) is stored as snapshot names + ids only;
    it is not resolved against dakavara_pa.constituency.
    """
    candidateName: str
    mobileNo: str
    gender: str
    age: int
    dob: str
    casteStateId: int
    casteName: Optional[str] = None
    casteCategoryName: Optional[str] = None
    occupationId: int
    occupationName: Optional[str] = None
    educationId: int
    educationName: Optional[str] = None
    parliamentId: Optional[int] = None
    parliamentName: str
    assemblyId: Optional[int] = None
    assemblyName: str
    mandalId: Optional[int] = None
    mandalName: str
    partyId: int
    partyShortName: str
    remarks: Optional[str] = None
    createdBy: int = 0
    createdByName: Optional[str] = None

class ProposalStatusUpdateRequest(BaseModel):
    statusCode: str
    goNumber: Optional[str] = None
    goDate: Optional[str] = None
    remarks: Optional[str] = None
    actionBy: int = 0
    actionByName: Optional[str] = None


class CompareCandidatesRequest(BaseModel):
    """Optional body for compare. Omit or send {} to compare all proposal candidates."""
    mids: Optional[List[str]] = None


class CasteUpdateRequest(BaseModel):
    """Update a candidate's caste by setting tdp_cadre.caste_state_id, keyed by membership_id."""
    mid: str
    casteStateId: int


class OccupationUpdateRequest(BaseModel):
    """Update a candidate's occupation by setting tdp_cadre.occupation_id, keyed by membership_id."""
    mid: str
    occupationId: int


class EducationUpdateRequest(BaseModel):
    """Update a candidate's education by setting tdp_cadre.education_id, keyed by membership_id."""
    mid: str
    educationId: int
