"""Pure-ASGI middleware (no BaseHTTPMiddleware: streaming responses stay streaming).

- RequestContext: accept or generate X-Request-ID, bind it to logs, echo it,
  write one access log line (request_id, principal, route, status, latency),
  and turn any unhandled exception into a generic 500 problem (no stack trace).
- SecurityHeaders: nosniff, X-Frame-Options DENY, Referrer-Policy, minimal CSP.
- BodySizeLimit: refuse bodies over MAX_REQUEST_SIZE (Content-Length up front,
  counted while streaming otherwise). Also enforce this at the reverse proxy.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.problems import PROBLEM_MEDIA_TYPE, problem_bytes
from app.logging_setup import bind_request_id, current_request_id, reset_request_id

log = logging.getLogger("app.api.access")

REQUEST_ID_HEADER: Final = "x-request-id"
_SAFE_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SECURITY_HEADERS: Final = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
)
DOCS_CSP: Final = (
    b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"img-src 'self' data: https://fastapi.tiangolo.com; "
    b"frame-ancestors 'none'"
)
DOCS_PATHS: Final = ("/docs", "/redoc")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, REQUEST_ID_HEADER.encode())
        request_id = incoming if incoming and _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = bind_request_id(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = {"code": 500, "started": False}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"], status["started"] = message["status"], True
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER.encode(), request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("unhandled error", extra={"route": scope.get("path")})
            if not status["started"]:
                await send_problem(
                    send_with_id,
                    500,
                    code="internal_error",
                    detail="An unexpected error occurred.",
                    instance=str(scope.get("path", "")),
                )
        finally:
            state: dict[str, Any] = scope.get("state", {})
            log.info(
                "request",
                extra={
                    "request_id": current_request_id(),
                    "principal": state.get("principal_name"),
                    "method": scope.get("method"),
                    "route": scope.get("path"),
                    "status": status["code"],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            reset_request_id(token)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = str(scope.get("path", "")).startswith(DOCS_PATHS)

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k for k, _ in message.get("headers", [])}
                extra = [
                    (k, DOCS_CSP if is_docs and k == b"content-security-policy" else v)
                    for k, v in SECURITY_HEADERS
                    if k not in present
                ]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_secured)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _too_large(scope, send, self.max_bytes)
            return
        seen = {"bytes": 0}

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                seen["bytes"] += len(message.get("body", b""))
                if seen["bytes"] > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, counting_receive, send)
        except _BodyTooLarge:
            await _too_large(scope, send, self.max_bytes)


class _BodyTooLarge(Exception):
    pass


async def send_problem(send: Send, status: int, *, code: str, detail: str, instance: str) -> None:
    body = problem_bytes(status, code=code, detail=detail, instance=instance)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", PROBLEM_MEDIA_TYPE.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _too_large(scope: Scope, send: Send, limit: int) -> None:
    await send_problem(
        send,
        413,
        code="request_too_large",
        detail=f"The request body exceeds {limit // (1024 * 1024)} MB.",
        instance=str(scope.get("path", "")),
    )
