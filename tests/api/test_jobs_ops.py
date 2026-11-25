"""Reprocessing (single, concurrent, bulk), reconciliation runs, search, stats, sweeper."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.container import Container
from app.models import Document, ProcessingJob
from app.models.enums import ApiRole, DocumentStatus, ErrorCode, JobStatus, JobType
from app.workers.dispatch import FromStage
from app.workers.sweeper import Sweeper
from tests.api.conftest import API, RecordingEnqueuer
from tests.api.seed import mismatched_total, seeded_invoice

Auth = Callable[[ApiRole], dict[str, str]]


def _fail(session: Session, document_id: uuid.UUID, code: ErrorCode) -> None:
    document = session.get_one(Document, document_id)
    document.status, document.error_code, document.error_message = DocumentStatus.FAILED, code, "x"
    document.processing_finished_at = datetime.now(UTC)
    session.add(
        ProcessingJob(
            document_id=document_id, job_type=JobType.FULL, status=JobStatus.FAILED, error_code=code
        )
    )
    session.commit()


# --------------------------------------------------------------- reprocess


@pytest.mark.parametrize(
    ("stage", "job_type"), [("parse", "reprocess"), ("extract", "reextract"), ("validate", "revalidate")]
)
def test_reprocess_creates_and_enqueues(
    client: TestClient,
    auth: Auth,
    db_session: Session,
    container: Container,
    enqueuer: RecordingEnqueuer,
    stage: str,
    job_type: str,
) -> None:
    document_id, _ = seeded_invoice(db_session, container)
    response = client.post(
        f"{API}/documents/{document_id}/reprocess",
        headers=auth(ApiRole.REVIEWER),
        json={"from_stage": stage, "force": True},
    )
    job = response.json()
    assert response.status_code == 202
    assert (job["job_type"], job["status"], job["triggered_by"]) == (job_type, "queued", "reviewer-key")
    assert enqueuer.jobs == [(document_id, uuid.UUID(job["id"]), FromStage(stage), True)]
    assert client.get(f"{API}/jobs/{job['id']}", headers=auth(ApiRole.VIEWER)).json()["id"] == job["id"]


def test_second_reprocess_is_409(
    client: TestClient, auth: Auth, db_session: Session, container: Container
) -> None:
    document_id, _ = seeded_invoice(db_session, container)
    url = f"{API}/documents/{document_id}/reprocess"
    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={}).status_code == 202
    second = client.post(url, headers=auth(ApiRole.REVIEWER), json={})
    assert second.status_code == 409 and second.json()["code"] == "job_active"


def test_concurrent_reprocess_only_one_wins(
    client: TestClient, auth: Auth, db_session: Session, container: Container, enqueuer: RecordingEnqueuer
) -> None:
    document_id, _ = seeded_invoice(db_session, container)
    headers = auth(ApiRole.REVIEWER)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(
            pool.map(
                lambda _: (
                    client.post(
                        f"{API}/documents/{document_id}/reprocess", headers=headers, json={}
                    ).status_code
                ),
                range(6),
            )
        )
    assert sorted(codes) == [202] + [409] * 5
    assert len(enqueuer.jobs) == 1


def test_active_job_uniqueness_is_enforced_by_the_database(
    session_factory: sessionmaker[Session], db_session: Session, container: Container
) -> None:
    document_id, _ = seeded_invoice(db_session, container, validate=False)
    with session_factory() as session:
        session.add_all(
            [
                ProcessingJob(document_id=document_id, job_type=JobType.REPROCESS),
                ProcessingJob(document_id=document_id, job_type=JobType.REPROCESS),
            ]
        )
        with pytest.raises(IntegrityError, match="uq_processing_jobs_document_id_active"):
            session.commit()


def test_reprocess_unknown_document_404(client: TestClient, auth: Auth) -> None:
    response = client.post(
        f"{API}/documents/{uuid.uuid4()}/reprocess", headers=auth(ApiRole.REVIEWER), json={}
    )
    assert response.status_code == 404


def test_bulk_reprocess_skips_permanent_errors(
    client: TestClient, auth: Auth, db_session: Session, container: Container, enqueuer: RecordingEnqueuer
) -> None:
    transient, _ = seeded_invoice(db_session, container, validate=False, minutes=1, number="T-1")
    permanent, _ = seeded_invoice(db_session, container, validate=False, minutes=2, number="P-1")
    _fail(db_session, transient, ErrorCode.LLM_ERROR)
    _fail(db_session, permanent, ErrorCode.PASSWORD_PROTECTED)
    url = f"{API}/jobs/reprocess-failed"

    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={}).status_code == 403
    body = client.post(url, headers=auth(ApiRole.ADMIN), json={"limit": 10}).json()
    outcomes = {i["document_id"]: i["outcome"] for i in body["items"]}
    assert (body["queued"], body["skipped"]) == (1, 1)
    assert outcomes == {str(transient): "queued", str(permanent): "skipped"}
    assert [d for d, *_ in enqueuer.jobs] == [transient]

    forced = client.post(
        url, headers=auth(ApiRole.ADMIN), json={"force": True, "error_codes": ["PASSWORD_PROTECTED"]}
    ).json()
    assert forced["queued"] == 1 and forced["items"][0]["document_id"] == str(permanent)


def test_bulk_limit_is_bounded(client: TestClient, auth: Auth) -> None:
    response = client.post(f"{API}/jobs/reprocess-failed", headers=auth(ApiRole.ADMIN), json={"limit": 501})
    assert response.status_code == 422


# ---------------------------------------------------------- reconciliation


def test_reconciliation_run_lifecycle(
    client: TestClient, auth: Auth, db_session: Session, container: Container, enqueuer: RecordingEnqueuer
) -> None:
    left, _ = seeded_invoice(db_session, container, validate=False, minutes=1)
    right, _ = seeded_invoice(db_session, container, validate=False, minutes=2, name="ledger.xlsx")
    response = client.post(
        f"{API}/reconciliations",
        headers=auth(ApiRole.REVIEWER),
        json={"left_document_ids": [str(left)], "right_document_ids": [str(right)]},
    )
    run_id = uuid.UUID(response.json()["run_id"])
    assert response.status_code == 202 and enqueuer.runs == [run_id]

    pending = client.get(f"{API}/reconciliations/{run_id}", headers=auth(ApiRole.VIEWER)).json()
    assert (pending["status"], pending["report"], pending["created_by"]) == ("running", {}, "reviewer-key")

    container.reconciliation_service().execute(run_id)  # what the Celery task does
    done = client.get(f"{API}/reconciliations/{run_id}", headers=auth(ApiRole.VIEWER)).json()
    assert done["status"] == "succeeded"
    assert done["report"]["counts"]["matched"] == 1


def test_reconciliation_rejects_unknown_documents(client: TestClient, auth: Auth) -> None:
    response = client.post(
        f"{API}/reconciliations",
        headers=auth(ApiRole.REVIEWER),
        json={"left_document_ids": [str(uuid.uuid4())], "right_document_ids": [str(uuid.uuid4())]},
    )
    assert response.status_code == 422 and response.json()["code"] == "unknown_documents"


# ------------------------------------------------------------------- stats


def test_stats(client: TestClient, auth: Auth, db_session: Session, container: Container) -> None:
    document_id, _ = mismatched_total(db_session, container)
    document = db_session.get_one(Document, document_id)
    document.processing_started_at = datetime.now(UTC) - timedelta(seconds=4)
    document.processing_finished_at = datetime.now(UTC)
    db_session.commit()
    body = client.get(f"{API}/stats", headers=auth(ApiRole.ADMIN)).json()
    assert body["documents_by_status"] == {"needs_review": 1}
    assert body["extractions_by_review_status"] == {"needs_review": 1}
    assert body["open_issues_by_rule"]["TOTAL_MISMATCH"] == 1
    assert body["processing_time_by_file_type"]["pdf"]["count"] == 1
    assert 3500 <= body["processing_time_by_file_type"]["pdf"]["p50_ms"] <= 4500
    assert body["last_24h"]["finished"] == 1


# ----------------------------------------------------------------- sweeper


def test_sweeper(
    session_factory: sessionmaker[Session],
    db_session: Session,
    container: Container,
    enqueuer: RecordingEnqueuer,
) -> None:
    now = datetime.now(UTC)
    stale_doc, _ = seeded_invoice(db_session, container, validate=False, minutes=1, number="S-1")
    deferred_doc, _ = seeded_invoice(db_session, container, validate=False, minutes=2, number="S-2")
    fresh_doc, _ = seeded_invoice(db_session, container, validate=False, minutes=3, number="S-3")
    running_doc, _ = seeded_invoice(db_session, container, validate=False, minutes=4, number="S-4")
    stale = ProcessingJob(
        document_id=stale_doc, job_type=JobType.REEXTRACT, queued_at=now - timedelta(hours=1)
    )
    deferred = ProcessingJob(
        document_id=deferred_doc,
        job_type=JobType.FULL,
        queued_at=now - timedelta(hours=1),
        error_code=ErrorCode.LLM_QUOTA_DEFERRED,
    )
    fresh = ProcessingJob(document_id=fresh_doc, job_type=JobType.FULL, queued_at=now)
    overdue = ProcessingJob(
        document_id=running_doc,
        job_type=JobType.FULL,
        status=JobStatus.RUNNING,
        started_at=now - timedelta(hours=3),
    )
    db_session.add_all([stale, deferred, fresh, overdue])
    db_session.commit()

    sweeper = Sweeper(
        session_factory=session_factory,
        enqueuer=enqueuer,
        stale_queued=timedelta(minutes=15),
        max_runtime=timedelta(minutes=120),
    )
    report = sweeper.run()
    assert report.requeued == [stale.id] and report.timed_out == [overdue.id]
    assert enqueuer.jobs == [(stale_doc, stale.id, FromStage.EXTRACT, False)]

    db_session.expire_all()
    assert db_session.get_one(ProcessingJob, overdue.id).status is JobStatus.FAILED
    failed_doc = db_session.get_one(Document, running_doc)
    assert (failed_doc.status, failed_doc.error_code) == (DocumentStatus.FAILED, ErrorCode.WORKER_TIMEOUT)

    again = sweeper.run()  # idempotent: queued_at was bumped, the overdue job is already failed
    assert again.requeued == [] and again.timed_out == []
