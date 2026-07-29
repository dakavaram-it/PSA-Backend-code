from typing import Any, Optional
from pydantic import BaseModel


class ApiResponse(BaseModel):
    success: bool = True
    data: Any
    message: str = "OK"


class NominatedFilters(BaseModel):
    enrollmentId: int = 2
    boardLevelId: int
    locationValue: int
    departmentId: Optional[int] = None
    boardId: Optional[int] = None
    positionId: Optional[int] = None
