"""The ONE place that maps errors to HTTP: RFC 9457 problem+json everywhere.

{type, title, status, detail, instance, code, request_id, errors[]}. Never a
stack trace: unexpected exceptions are caught by RequestContextMiddleware
(inside the request-id scope, so the 500 still carries the id), logged, and
answered with a generic body built here.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import errors
from app.logging_setup import current_request_id

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
TYPE_BASE: Final = "https://careguide.dev/problems/"
STATUS: Final[dict[type[errors.DomainError], int]] = {
    errors.BadRequest: 400,
    errors.Unauthorized: 401,
    errors.Forbidden: 403,
    errors.NotFound: 404,
    errors.Conflict: 409,
    errors.PreconditionFailed: 412,
    errors.PayloadTooLarge: 413,
    errors.ValidationFailed: 422,
    errors.PreconditionRequired: 428,
    errors.TooManyRequests: 429,
    errors.NotImplementedYet: 501,
    errors.ServiceUnavailable: 503,
}


def problem_body(
    status: int, *, code: str, detail: str, instance: str, field_errors: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "type": f"{TYPE_BASE}{code}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code,
        "request_id": current_request_id(),
        "errors": field_errors or [],
    }


def problem_bytes(status: int, *, code: str, detail: str, instance: str) -> bytes:
    """For raw ASGI middleware, which can't use JSONResponse."""
    return json.dumps(problem_body(status, code=code, detail=detail, instance=instance)).encode()


def problem(
    status: int,
    *,
    code: str,
    detail: str,
    instance: str,
    field_errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = problem_body(status, code=code, detail=detail, instance=instance, field_errors=field_errors)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


def _domain(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.DomainError)
    status = next(s for cls, s in STATUS.items() if isinstance(exc, cls))
    return problem(
        status,
        code=exc.code,
        detail=exc.detail,
        instance=request.url.path,
        field_errors=[e.to_dict() for e in exc.errors],
        headers=exc.headers or None,
    )


def _validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    field_errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", "invalid"), "type": e.get("type", "value_error")}
        for e in exc.errors()
    ]
    return problem(
        422,
        code="validation_error",
        detail="The request has invalid fields.",
        instance=request.url.path,
        field_errors=field_errors,
    )


def _http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
    detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).description
    return problem(
        exc.status_code,
        code=code,
        detail=detail,
        instance=request.url.path,
        headers=dict(exc.headers) if exc.headers else None,
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(errors.DomainError, _domain)
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(StarletteHTTPException, _http)
