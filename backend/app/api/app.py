"""App factory: create_app(settings, container) builds an isolated app (tests make their own).

Middleware, outermost first:
  SecurityHeaders -> RequestContext (request id, access log, 500 safety net)
  -> GZip -> CORS (explicit allowlist) -> BodySizeLimit -> routes
Every response, including 413/500 from middleware, therefore carries the
request id and the security headers.

    uvicorn app.api.app:main --factory
    gunicorn -k uvicorn.workers.UvicornWorker -w $WEB_CONCURRENCY 'app.api.app:main()'
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import problems
from app.api.container import Container, build_container
from app.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.routers import documents, extractions, ops
from app.config import ApiSettings, get_api_settings
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)

API_PREFIX: Final = "/api/v1"
GZIP_MIN_BYTES: Final = 1024
CORS_MAX_AGE_S: Final = 600
API_VERSION: Final = "1.0.0"
DESCRIPTION: Final = """Document-intelligence API: upload invoices, track processing, review and correct
extracted fields, reconcile documents and search.

**Auth**: `Authorization: Bearer <api key>` on every route except `/health/*`.
**Errors**: RFC 9457 `application/problem+json`. **Money**: decimal strings. See docs/api.md.
"""


def create_app(settings: ApiSettings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_api_settings()
    if "*" in settings.cors_origins:
        raise ValueError("CORS_ORIGINS must be an explicit allowlist; '*' is not allowed")
    container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Graceful shutdown: uvicorn stops accepting, drains in-flight requests,
        then this closes pooled connections."""
        yield
        container.redis.close()
        container.session_factory.kw["bind"].dispose()
        log.info("api stopped")

    app = FastAPI(
        title="CareGuide Document Intelligence API",
        version=API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.container = container
    problems.install(app)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(documents.router)
    api.include_router(extractions.router)
    api.include_router(ops.jobs)
    api.include_router(ops.reconciliations)
    api.include_router(ops.search)
    app.include_router(api)
    app.include_router(ops.health)

    # add_middleware wraps: the LAST one added is the OUTERMOST.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_size_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "If-Match",
            "If-None-Match",
            "Idempotency-Key",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "X-Request-ID", "Retry-After"],
        max_age=CORS_MAX_AGE_S,
    )
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def main() -> FastAPI:
    """Entry for `uvicorn app.api.app:main --factory` with JSON logging configured."""
    configure_logging()
    return create_app()
