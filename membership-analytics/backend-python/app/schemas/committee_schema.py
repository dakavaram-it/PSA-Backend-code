from typing import Any, Optional, List
from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    success: bool = True
    message: str = "OK"
    data: Any = None


class CommitteePositionRequest(BaseModel):
    legacyTdpCommitteeRoleId: Optional[int] = None
    legacyTdpRolesId: Optional[int] = None
    roleName: str
    roleType: Optional[str] = None
    seatsRequired: int = Field(default=1, ge=1)
    maxMembersSnapshot: Optional[int] = None
    minProposeMembersSnapshot: Optional[int] = None
    maxProposeMembersSnapshot: Optional[int] = None
    alreadyProposedSnapshot: int = 0
    alreadySelectedSnapshot: int = 0


class CommitteeProposalCreateRequest(BaseModel):
    committeeTypeCode: str
    legacyTdpCommitteeId: Optional[int] = None
    legacyTdpBasicCommitteeId: Optional[int] = None
    legacyTdpCommitteeLevelId: Optional[int] = None
    legacyTdpCommitteeLevelValue: Optional[int] = None
    legacyTdpCommitteeEnrollmentId: Optional[int] = None
    legacyCommitteeConfirmRuleId: Optional[int] = None
    committeeName: Optional[str] = None
    committeeLevelName: Optional[str] = None
    locationName: Optional[str] = None
    remarks: Optional[str] = None
    createdBy: int
    createdByName: Optional[str] = None
    positions: List[CommitteePositionRequest]


class CommitteeCompareRequest(BaseModel):
    mids: Optional[List[str]] = None


class CommitteeStatusUpdateRequest(BaseModel):
    statusCode: str
    actionBy: int = 0
    actionByName: Optional[str] = None
    remarks: Optional[str] = None


class CommitteeMemberItem(BaseModel):
    committeeProposalPositionId: int
    tdpCadreId: int
    membershipId: Optional[str] = None
    candidateName: Optional[str] = None
    mobileNo: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    casteStateId: Optional[int] = None
    sourceType: str = "CADRE_SEARCH"
    remarks: Optional[str] = None


class CommitteeMembersAddRequest(BaseModel):
    createdBy: int
    createdByName: Optional[str] = None
    members: List[CommitteeMemberItem]


class CommitteeFeedbackItem(BaseModel):
    committeeProposalMemberId: int
    feedbackCode: str
    feedbackText: Optional[str] = None
    noFeedbackRequired: str = "N"


class CommitteeReviewItem(BaseModel):
    committeeProposalMemberId: int
    reviewerRoleCode: str
    rankValue: Optional[float] = None
    reviewStatus: str = "REVIEWED"
    reviewComments: Optional[str] = None


class CommitteeWorkflowActionRequest(BaseModel):
    actionCode: str
    roleCode: str = "ADMIN"
    actionBy: int
    actionByName: Optional[str] = None
    comments: Optional[str] = None
    remarks: Optional[str] = None
    selectedCommitteeProposalMemberIds: List[int] = []
    feedbacks: List[CommitteeFeedbackItem] = []
    reviews: List[CommitteeReviewItem] = []


class CommitteeFeedbackSaveRequest(BaseModel):
    """Persist feedback without a stage change — mirrors the Nominated Post
    POST /workflow/proposals/{id}/feedbacks so feedback edited after the proposal
    has moved past the FEEDBACK stage is still saved (committee otherwise only
    persists feedback inline on the MOVE_TO_REVIEW transition)."""
    feedbacks: List[CommitteeFeedbackItem] = []
    actionBy: int = 0
    actionByName: Optional[str] = None


class CommitteeReviewSaveRequest(BaseModel):
    """Persist reviews without a stage change — mirrors the Nominated Post
    PUT /workflow/proposals/{id}/reviews so reviews typed in the Review step are
    saved as they're entered (committee otherwise only persists reviews inline on
    the MOVE_TO_FINALISING / FINALISE_COMMITTEE transition, so an F5 before moving
    lost them)."""
    reviews: List[CommitteeReviewItem] = []
    actionBy: int = 0
    actionByName: Optional[str] = None
