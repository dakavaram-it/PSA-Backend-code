from typing import Any, Optional

from pydantic import BaseModel, Field


class CadreProfileDto(BaseModel):
    tdpCadreId: Optional[int] = None
    membershipId: Optional[str] = None
    membershipNo: Optional[str] = None
    mid: Optional[str] = None
    candidateName: Optional[str] = None
    mobileNo: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[str] = None
    occupation: Optional[str] = None
    renewalTimes: Optional[int] = None
    mandal: Optional[str] = None
    village: Optional[str] = None
    assembly: Optional[str] = None
    parliament: Optional[str] = None
    casteStateId: Optional[int] = None
    casteName: Optional[str] = None
    castCategory: Optional[str] = None
    designation: Optional[str] = None
    constituencyPercent: Optional[float] = None
    castePercentage: Optional[float] = None
    photo: Optional[str] = None
    photoUrl: Optional[str] = None


class CadrePerformanceReportDto(BaseModel):
    membershipId: Optional[str] = None
    performanceScore: Optional[float] = None
    pedalaSevalo: Optional[int] = None
    pedalaSevaloPoints: Optional[float] = None
    firstMembershipYear: Optional[str] = None
    firstMembershipPoints: Optional[float] = None
    renewalTimes: Optional[int] = None
    renewalPoints: Optional[float] = None
    referrals: Optional[int] = None
    referralPoints: Optional[float] = None
    mandalVoteSharePercent: Optional[float] = None
    mandalVoteSharePoints: Optional[float] = None
    boothVoteSharePercent: Optional[float] = None
    boothVoteSharePoints: Optional[float] = None
    mandalMembershipAchPercent: Optional[float] = None
    mandalMembershipPoints: Optional[float] = None
    boothMembershipAchPercent: Optional[float] = None
    boothMembershipPoints: Optional[float] = None
    mandalD2dAchPercent: Optional[float] = None
    mandalD2dPoints: Optional[float] = None
    boothD2dAchPercent: Optional[float] = None
    boothD2dPoints: Optional[float] = None
    positions2018_2020: Optional[str] = None
    positions2016_2018: Optional[str] = None
    positions2014_2016: Optional[str] = None
    positionsPoints: Optional[float] = None
    raw: Optional[dict[str, Any]] = None


class CadreCompareCandidateDto(BaseModel):
    membershipId: str
    mid: str
    tdpCadreId: Optional[int] = None
    proposalCandidateId: Optional[int] = None
    photoUrl: Optional[str] = None
    dakavaraProfile: Optional[dict[str, Any]] = None
    cadreDetails: Optional[dict[str, Any]] = None
    cadrePerformanceReport: Optional[dict[str, Any]] = None
    profile: Optional[dict[str, Any]] = None
    performance: Optional[dict[str, Any]] = None


class CadreCompareResponseDto(BaseModel):
    proposalId: int
    mids: list[str] = Field(default_factory=list)
    candidates: list[CadreCompareCandidateDto] = Field(default_factory=list)
