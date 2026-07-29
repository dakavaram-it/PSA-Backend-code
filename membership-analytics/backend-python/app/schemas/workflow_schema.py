from typing import Any, Optional, List
from pydantic import BaseModel

class ApiResponse(BaseModel):
    success: bool = True
    message: str = "OK"
    data: Any = None
# Candidate ID is mandatory
class WorkflowFeedbackItem(BaseModel):
    proposalCandidateId: int
    feedbackCode: str
    feedbackText: Optional[str] = None
    noFeedbackRequired: str = "N"

class WorkflowReviewItem(BaseModel):
    proposalCandidateId: int
    reviewerRoleCode: str
    rankValue: Optional[float] = None
    reviewStatus: str = "REVIEWED"
    reviewComments: Optional[str] = None

class WorkflowGoDetails(BaseModel):
    proposalCandidateId: int
    goNumber: str
    goIssueDate: str
    goRemarks: Optional[str] = None

class SaveReviewsRequest(BaseModel):
    reviews: List[WorkflowReviewItem]
    actionBy: int = 0
    actionByName: Optional[str] = None

class WorkflowFeedbackSaveRequest(BaseModel):
    actionBy: int = 0
    actionByName: Optional[str] = None
    feedbacks: List[WorkflowFeedbackItem]

class CandidateDecision(BaseModel):
    proposalCandidateId: int
    status: str  # SHORTLISTED | ON_HOLD | REJECTED | NOT_SELECTED | ADDED | SELECTED

class WorkflowActionRequest(BaseModel):
    actionCode: str
    roleCode: str = "ADMIN"
    actionBy: int
    actionByName: Optional[str] = None
    comments: Optional[str] = None
    remarks: Optional[str] = None
    selectedProposalCandidateId: Optional[int] = None
    selectedProposalCandidateIds: List[int] = []
    feedbacks: List[WorkflowFeedbackItem] = []
    reviews: List[WorkflowReviewItem] = []
    candidateDecisions: List[CandidateDecision] = []
    goDetails: Optional[WorkflowGoDetails] = None
