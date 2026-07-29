import logging
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    def __init__(self, message: str, status_code: int = 400, error_code: str = "APP_ERROR"):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


async def app_exception_handler(request: Request, exc: AppException):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.warning("request_id=%s app_error=%s message=%s", request_id, exc.error_code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "errorCode": exc.error_code, "message": exc.message, "requestId": request_id},
    )


async def generic_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.exception("request_id=%s unhandled_error=%s", request_id, str(exc))
    return JSONResponse(
        status_code=500,
        content={"success": False, "errorCode": "INTERNAL_SERVER_ERROR", "message": "Unexpected server error", "requestId": request_id},
    )
