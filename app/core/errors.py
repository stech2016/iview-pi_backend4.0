"""Standardised error envelope: {"error": {"code": "...", "message": "..."}}"""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

log = get_logger("errors")


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code, self.code, self.message = status_code, code, message
        super().__init__(message)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))


_HTTP_MESSAGES = {
    404: ("NOT_FOUND", "The requested resource was not found. Check the address and try again."),
    405: ("METHOD_NOT_ALLOWED", "This action is not supported for the requested resource."),
}


def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code, msg = _HTTP_MESSAGES.get(exc.status_code, ("HTTP_ERROR", str(exc.detail)))
    return JSONResponse(status_code=exc.status_code, content=error_body(code, msg))


def validation_handler(request: Request, exc: RequestValidationError):
    errs = exc.errors()
    first = errs[0] if errs else {"loc": [], "msg": "invalid input"}
    loc = ".".join(str(p) for p in first["loc"] if p not in ("body", "query", "path")) or "request"
    msg = str(first["msg"]).removeprefix("Value error, ")
    return JSONResponse(
        status_code=422,
        content=error_body("VALIDATION_ERROR", f"Invalid value for '{loc}': {msg}. Please correct it and try again."),
    )


def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content=error_body("RATE_LIMITED", "Too many requests. Please wait a minute and try again."),
    )


def unhandled_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR", "Something went wrong on our side. Please try again; if it persists contact your administrator."),
    )
