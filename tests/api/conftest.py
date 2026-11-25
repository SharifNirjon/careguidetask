"""API test fixtures: real PostgreSQL, fakeredis, local storage in tmp, and a
recording enqueuer (nothing reaches Celery; tests assert what WOULD be enqueued).

Services commit for real, so these tests use `committed_engine` and wipe the
tables afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.app import create_app
from app.api.container import Container
from app.api.ratelimit import ApiRateLimiter
from app.api.services.auth import AuthService
from app.api.services.search import KeywordSearchService
from app.config import ApiSettings, PipelineSettings, ValidationSettings
from app.models.enums import ApiRole
from app.pipeline.storage import LocalStorage
from app.workers.dispatch import FromStage
from tests.fixtures import factory

ALLOWED_ORIGIN = "http://frontend.test"
API = "/api/v1"


@dataclass
class RecordingEnqueuer:
    jobs: list[tuple[uuid.UUID, uuid.UUID, FromStage, bool]] = field(default_factory=list)
    runs: list[uuid.UUID] = field(default_factory=list)
    fail: bool = False

    def enqueue(
        self, document_id: uuid.UUID, job_id: uuid.UUID, stage: FromStage, *, force: bool = False
    ) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.jobs.append((document_id, job_id, stage, force))

    def enqueue_reconciliation(self, run_id: uuid.UUID) -> None:
        self.runs.append(run_id)


@pytest.fixture(scope="session")
def files() -> dict[str, bytes]:
    """Generated synthetic inputs (no real invoices anywhere in the tests)."""
    return {
        "invoice.pdf": factory.digital_pdf([factory.invoice_lines("INV-2024-001")]),
        "scan.png": factory.blurry_png(),
        "ledger.xlsx": factory.invoice_xlsx(),
        "frames.tiff": factory.multipage_tiff(),
        "corrupt.pdf": b"%PDF-1.7\n" + b"\x00garbage" * 50,
        "renamed_exe.pdf": b"MZ\x90\x00" + b"\x00" * 60 + b"This program cannot be run in DOS mode",
    }


@pytest.fixture
def session_factory(committed_engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=committed_engine, expire_on_commit=False)
    with committed_engine.begin() as conn:
        conn.execute(text("TRUNCATE api_keys"))


@pytest.fixture
def enqueuer() -> RecordingEnqueuer:
    return RecordingEnqueuer()


@pytest.fixture
def api_settings() -> ApiSettings:
    return ApiSettings(environment="test", cors_origins=[ALLOWED_ORIGIN], max_files_per_upload=5)


@pytest.fixture
def make_container(
    session_factory: sessionmaker[Session], enqueuer: RecordingEnqueuer, tmp_path: Path
) -> Callable[..., Container]:
    def build(api: ApiSettings, **overrides: Any) -> Container:
        redis = fakeredis.FakeRedis()
        pipeline = PipelineSettings(storage_root=tmp_path / "storage", max_file_size_mb=2)
        pipeline.storage_root.mkdir(parents=True, exist_ok=True)
        values: dict[str, Any] = {
            "api": api,
            "pipeline": pipeline,
            "validation": ValidationSettings(),
            "session_factory": session_factory,
            "redis": redis,
            "storage": LocalStorage(pipeline.storage_root),
            "enqueuer": enqueuer,
            "search": KeywordSearchService(min_similarity=api.search_min_similarity),
            "rate_limiter": ApiRateLimiter(redis, window_s=api.rate_window_s),
            "health_checks": {"database": lambda: None, "redis": redis.ping, "storage": lambda: None},
        }
        return Container(**{**values, **overrides})

    return build


@pytest.fixture
def container(make_container: Callable[..., Container], api_settings: ApiSettings) -> Container:
    return make_container(api_settings)


@pytest.fixture
def app(container: Container, api_settings: ApiSettings) -> FastAPI:
    return create_app(api_settings, container)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def keys(session_factory: sessionmaker[Session]) -> dict[ApiRole, str]:
    with session_factory() as session:
        service = AuthService(session)
        return {role: service.create_key(f"{role.value}-key", role).key for role in ApiRole}


@pytest.fixture
def auth(keys: dict[ApiRole, str]) -> Callable[[ApiRole], dict[str, str]]:
    return lambda role: {"Authorization": f"Bearer {keys[role]}"}


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
