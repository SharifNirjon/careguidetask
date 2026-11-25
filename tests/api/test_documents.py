"""Uploads, listing (filters, sort, keyset pagination), detail/progress, events,
file download, page previews, results + ETags, soft delete."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.container import Container
from app.models import Document, DocumentUnit, ProcessingEvent, ProcessingJob
from app.models.enums import (
    ApiRole,
    EventStatus,
    ExtractionMethod,
    JobStatus,
    Stage,
    UnitType,
)
from app.workers.dispatch import FromStage
from tests.api.conftest import API, RecordingEnqueuer
from tests.api.seed import mismatched_total, seeded_invoice

Auth = Callable[[ApiRole], dict[str, str]]
MIME = {
    "pdf": "application/pdf",
    "png": "image/png",
    "tiff": "image/tiff",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _part(name: str, data: bytes) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, data, "application/octet-stream"))


def _upload(client: TestClient, auth: Auth, parts: list[Any], **headers: str) -> Any:
    return client.post(f"{API}/documents", headers={**auth(ApiRole.REVIEWER), **headers}, files=parts)


# ------------------------------------------------------------------ upload


def test_mixed_upload_gives_per_file_outcomes(
    client: TestClient, auth: Auth, files: dict[str, bytes], enqueuer: RecordingEnqueuer
) -> None:
    names = ["invoice.pdf", "scan.png", "ledger.xlsx", "corrupt.pdf"]
    response = _upload(client, auth, [_part(n, files[n]) for n in names])
    outcomes = {o["filename"]: o for o in response.json()}
    assert response.status_code == 202
    assert [outcomes[n]["status"] for n in names[:3]] == ["queued"] * 3
    assert all(uuid.UUID(outcomes[n]["document_id"]) for n in names[:3])
    corrupt = outcomes["corrupt.pdf"]
    assert corrupt["status"] in ("queued", "rejected")  # a damaged PDF body is detected at parse time
    assert len(enqueuer.jobs) == sum(o["status"] == "queued" for o in outcomes.values())
    assert {stage for *_, stage, _ in enqueuer.jobs} == {FromStage.PARSE}


def test_renamed_exe_is_type_mismatch(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    outcome = _upload(
        client,
        auth,
        [_part("renamed_exe.pdf", files["renamed_exe.pdf"]), _part("invoice.pdf", files["invoice.pdf"])],
    ).json()
    assert outcome[0]["status"] == "rejected"
    assert outcome[0]["error"]["code"] == "TYPE_MISMATCH"
    assert outcome[0]["document_id"] is None
    assert outcome[1]["status"] == "queued"  # one bad file never fails the batch


def test_oversized_file_is_413_and_ingests_nothing(
    client: TestClient, auth: Auth, files: dict[str, bytes], enqueuer: RecordingEnqueuer, db_session: Session
) -> None:
    big = b"%PDF-1.7\n" + b"0" * (3 * 1024 * 1024)  # container limit: 2 MB
    response = _upload(client, auth, [_part("invoice.pdf", files["invoice.pdf"]), _part("big.pdf", big)])
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert enqueuer.jobs == [] and db_session.query(Document).count() == 0


def test_too_many_files_is_400(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    response = _upload(client, auth, [_part(f"f{i}.pdf", files["invoice.pdf"]) for i in range(6)])
    assert response.status_code == 400
    assert response.json()["code"] == "too_many_files"


def test_idempotency_key_replay(
    client: TestClient, auth: Auth, files: dict[str, bytes], enqueuer: RecordingEnqueuer, db_session: Session
) -> None:
    parts = [_part("invoice.pdf", files["invoice.pdf"])]
    first = _upload(client, auth, parts, **{"Idempotency-Key": "upload-0001"})
    replay = _upload(client, auth, parts, **{"Idempotency-Key": "upload-0001"})
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert db_session.query(Document).count() == 1 and len(enqueuer.jobs) == 1
    other = _upload(client, auth, parts, **{"Idempotency-Key": "upload-0002"})
    assert other.json()[0]["document_id"] != first.json()[0]["document_id"]


def test_invalid_idempotency_key_is_422(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    response = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])], **{"Idempotency-Key": "x"})
    assert response.status_code == 422


def test_enqueue_failure_still_202(
    client: TestClient, auth: Auth, files: dict[str, bytes], enqueuer: RecordingEnqueuer, db_session: Session
) -> None:
    enqueuer.fail = True  # commit THEN enqueue: the job row waits for the sweeper
    response = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])])
    assert response.status_code == 202 and response.json()[0]["status"] == "queued"
    assert db_session.query(ProcessingJob).one().status is JobStatus.QUEUED


def test_duplicate_upload_links_original(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    first = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]
    second = _upload(client, auth, [_part("b.pdf", files["invoice.pdf"])]).json()[0]
    assert second["duplicate_of_document_id"] == first["document_id"]


# -------------------------------------------------------------------- list


@pytest.fixture
def catalog(db_session: Session, container: Container) -> dict[str, uuid.UUID]:
    ids = {}
    ids["acme"], _ = seeded_invoice(
        db_session, container, minutes=1, number="INV-A1", total="270.00", issued=date(2024, 3, 1)
    )
    ids["globex"], _ = seeded_invoice(
        db_session,
        container,
        minutes=2,
        number="GX-77",
        supplier="Globex Corp",
        customer="Acme Hospital",
        total="99.00",
        subtotal="90.00",
        tax="9.00",
        currency="USD",
        lines=[],
        issued=date(2024, 2, 1),
    )
    ids["broken"], _ = mismatched_total(
        db_session, container, minutes=3, number="INV-B2", supplier="Initech LLC", issued=date(2024, 1, 1)
    )
    return ids


def _ids(client: TestClient, auth: Auth, **params: Any) -> list[str]:
    response = client.get(f"{API}/documents", params=params, headers=auth(ApiRole.VIEWER))
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"supplier": "acme supplies"}, ["acme"]),
        ({"supplier": "Globex"}, ["globex"]),
        ({"customer": "hospital"}, ["globex"]),
        ({"invoice_number": "inv a1"}, ["acme"]),
        ({"currency": "usd"}, ["globex"]),
        ({"total_min": "100", "total_max": "280"}, ["acme"]),
        ({"invoice_date_from": "2024-02-01", "invoice_date_to": "2024-02-28"}, ["globex"]),
        ({"has_open_issues": "true", "rule_code": "TOTAL_MISMATCH"}, ["broken"]),
        ({"review_status": "needs_review"}, ["broken"]),
        ({"status": "needs_review"}, ["broken"]),
        ({"file_type": "xlsx"}, []),
        ({"uploaded_from": "2024-03-10T12:02:00Z"}, ["broken", "globex"]),
    ],
)
def test_filters(
    client: TestClient, auth: Auth, catalog: dict[str, uuid.UUID], params: dict[str, str], expected: list[str]
) -> None:
    assert _ids(client, auth, **params) == [str(catalog[k]) for k in expected]


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [
        ("created_at", "desc", ["broken", "globex", "acme"]),
        ("created_at", "asc", ["acme", "globex", "broken"]),
        ("total", "asc", ["globex", "acme", "broken"]),
        ("invoice_date", "desc", ["acme", "globex", "broken"]),
    ],
)
def test_sorting(
    client: TestClient, auth: Auth, catalog: dict[str, uuid.UUID], sort: str, order: str, expected: list[str]
) -> None:
    assert _ids(client, auth, sort=sort, order=order) == [str(catalog[k]) for k in expected]


def test_list_item_shape(client: TestClient, auth: Auth, catalog: dict[str, uuid.UUID]) -> None:
    item = client.get(
        f"{API}/documents", params={"invoice_number": "INV-B2"}, headers=auth(ApiRole.VIEWER)
    ).json()["items"][0]
    invoice = item["invoices"][0]
    assert invoice["open_issues"]["error"] >= 1
    assert "line_items" not in invoice and "text" not in item


@pytest.mark.parametrize("sort", ["created_at", "total", "invoice_date"])
def test_keyset_pagination_is_stable_across_inserts(
    client: TestClient, auth: Auth, db_session: Session, container: Container, sort: str
) -> None:
    for i in range(5):
        seeded_invoice(
            db_session,
            container,
            validate=False,
            minutes=i,
            number=f"P-{i}",
            total=f"{10 + i}.00",
            subtotal=None,
            tax=None,
            lines=[],
        )
    headers = auth(ApiRole.VIEWER)
    first = client.get(f"{API}/documents", params={"limit": 2, "sort": sort}, headers=headers).json()
    seeded_invoice(
        db_session,
        container,
        validate=False,
        minutes=-10,
        number="P-new",
        total="1.00",
        subtotal=None,
        tax=None,
        lines=[],
    )  # lands on page 1 in every ordering, never page 2
    seen = [i["id"] for i in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(
            f"{API}/documents", params={"limit": 2, "sort": sort, "cursor": cursor}, headers=headers
        ).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) >= 5


def test_invalid_cursor_is_422(client: TestClient, auth: Auth) -> None:
    response = client.get(f"{API}/documents", params={"cursor": "not-a-cursor"}, headers=auth(ApiRole.VIEWER))
    assert response.status_code == 422 and response.json()["code"] == "invalid_cursor"


# ------------------------------------------------------- detail & results


def test_detail_progress_and_etag(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]["document_id"]
    doc = db_session.get_one(Document, uuid.UUID(document_id))
    job = db_session.query(ProcessingJob).filter_by(document_id=doc.id).one()
    doc.page_count, job.status = 4, JobStatus.RUNNING
    job.started_at = doc.created_at
    db_session.add(
        ProcessingEvent(
            job_id=job.id,
            document_id=doc.id,
            stage=Stage.PARSE,
            status=EventStatus.SUCCEEDED,
            details={"scope": "batch", "units": 2},
        )
    )
    db_session.commit()

    headers = auth(ApiRole.VIEWER)
    response = client.get(f"{API}/documents/{document_id}", headers=headers)
    body = response.json()
    assert body["latest_job"]["status"] == "running"
    assert body["progress"] == {
        **body["progress"],
        "stage": "parse",
        "stages_done": 0,
        "stages_total": 5,
        "units_done": 2,
        "units_total": 4,
        "percent": 10,
    }
    etag = response.headers["etag"]
    assert (
        client.get(f"{API}/documents/{document_id}", headers={**headers, "If-None-Match": etag}).status_code
        == 304
    )


def test_unknown_document_is_404(client: TestClient, auth: Auth) -> None:
    response = client.get(f"{API}/documents/{uuid.uuid4()}", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 404 and response.json()["code"] == "document_not_found"
    assert client.get(f"{API}/documents/not-a-uuid", headers=auth(ApiRole.VIEWER)).status_code == 422


def test_events_are_paginated(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]["document_id"]
    job = db_session.query(ProcessingJob).one()
    for status in (EventStatus.STARTED, EventStatus.SUCCEEDED):
        db_session.add(
            ProcessingEvent(
                job_id=job.id,
                document_id=job.document_id,
                stage=Stage.PARSE,
                status=status,
                details={"error": "internal /path", "units": 1},
            )
        )
    db_session.commit()
    headers = auth(ApiRole.VIEWER)
    first = client.get(f"{API}/documents/{document_id}/events", params={"limit": 2}, headers=headers).json()
    second = client.get(
        f"{API}/documents/{document_id}/events",
        params={"limit": 2, "cursor": first["next_cursor"]},
        headers=headers,
    ).json()
    stages = [e["stage"] for e in first["items"] + second["items"]]
    assert stages == ["ingest", "parse", "parse"] and second["next_cursor"] is None
    assert "error" not in second["items"][0]["details"]  # internal failure details never leave the server


def test_results_shape_and_etag(
    client: TestClient, auth: Auth, db_session: Session, container: Container
) -> None:
    document_id, extraction_id = mismatched_total(db_session, container)
    headers = auth(ApiRole.VIEWER)
    response = client.get(f"{API}/documents/{document_id}/results", headers=headers)
    extraction = response.json()["extractions"][0]
    assert extraction["id"] == str(extraction_id)
    assert set(extraction["fields"]["total"]) == {
        "value",
        "raw_value",
        "confidence",
        "grounded",
        "evidence",
        "unit_index",
        "source",
    }
    assert [i["rule_code"] for i in extraction["issues"]].count("TOTAL_MISMATCH") == 1
    assert len(extraction["line_items"]) == 2
    etag = response.headers["etag"]
    assert (
        client.get(
            f"{API}/documents/{document_id}/results", headers={**headers, "If-None-Match": etag}
        ).status_code
        == 304
    )


# ------------------------------------------------------------ file & image


def test_file_download_headers(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    name = 'in"voice\r\nX-Evil: 1 ünïcode.pdf'
    document_id = _upload(client, auth, [_part(name, files["invoice.pdf"])]).json()[0]["document_id"]
    response = client.get(f"{API}/documents/{document_id}/file", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 200
    assert response.content == files["invoice.pdf"]
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline; filename=") and "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert "x-evil" not in {k.lower() for k in response.headers}


def _add_unit(db_session: Session, document_id: str, unit_type: UnitType, index: int = 0) -> None:
    db_session.add(
        DocumentUnit(
            document_id=uuid.UUID(document_id),
            unit_type=unit_type,
            unit_index=index,
            extraction_method=ExtractionMethod.DIGITAL,
        )
    )
    db_session.commit()


def test_page_image_renders_and_caches(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session, container: Container
) -> None:
    document_id = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]["document_id"]
    _add_unit(db_session, document_id, UnitType.PDF_PAGE)
    url = f"{API}/documents/{document_id}/units/0/image"
    first = client.get(url, params={"dpi": 72}, headers=auth(ApiRole.VIEWER))
    assert first.status_code == 200 and first.headers["content-type"] == "image/png"
    assert first.content.startswith(b"\x89PNG")
    assert client.get(url, params={"dpi": 72}, headers=auth(ApiRole.VIEWER)).content == first.content


@pytest.mark.parametrize("dpi", [10, 49, 201, 1200])
def test_page_image_dpi_bounds(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session, dpi: int
) -> None:
    document_id = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]["document_id"]
    _add_unit(db_session, document_id, UnitType.PDF_PAGE)
    response = client.get(
        f"{API}/documents/{document_id}/units/0/image", params={"dpi": dpi}, headers=auth(ApiRole.VIEWER)
    )
    assert response.status_code == 422


def test_image_frame_preview(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("f.tiff", files["frames.tiff"])]).json()[0]["document_id"]
    _add_unit(db_session, document_id, UnitType.IMAGE_FRAME, index=2)
    response = client.get(f"{API}/documents/{document_id}/units/2/image", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")


def test_sheet_unit_table(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("l.xlsx", files["ledger.xlsx"])]).json()[0]["document_id"]
    db_session.add(
        DocumentUnit(
            document_id=uuid.UUID(document_id),
            unit_type=UnitType.SHEET,
            unit_index=0,
            sheet_name="Ledger",
            extraction_method=ExtractionMethod.SPREADSHEET,
            structured_data={
                "rows": [
                    {"Invoice": "INV-1", "Total": "10.00", "_row": 2, "_block": 0},
                    {"Invoice": "INV-2", "Total": "<b>20</b>", "_row": 3, "_block": 0},
                ],
                "table_blocks": [{"index": 0, "headers": ["Invoice", "Total"]}],
            },
        )
    )
    db_session.commit()
    body = client.get(f"{API}/documents/{document_id}/units/0/table", headers=auth(ApiRole.VIEWER)).json()
    assert body == {
        "unit_index": 0,
        "sheet_name": "Ledger",
        "truncated": False,
        "blocks": [
            {
                "headers": ["Invoice", "Total"],
                "rows": [
                    {"row_number": 2, "cells": ["INV-1", "10.00"]},
                    {"row_number": 3, "cells": ["INV-2", "<b>20</b>"]},
                ],
            }
        ],
    }


def test_page_unit_has_no_table(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("a.pdf", files["invoice.pdf"])]).json()[0]["document_id"]
    _add_unit(db_session, document_id, UnitType.PDF_PAGE)
    response = client.get(f"{API}/documents/{document_id}/units/0/table", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 404


def test_sheet_unit_has_no_image(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session
) -> None:
    document_id = _upload(client, auth, [_part("l.xlsx", files["ledger.xlsx"])]).json()[0]["document_id"]
    _add_unit(db_session, document_id, UnitType.SHEET)
    response = client.get(f"{API}/documents/{document_id}/units/0/image", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 404 and response.json()["code"] == "unit_not_found"


# ------------------------------------------------------------------ delete


def test_soft_delete_is_admin_only(
    client: TestClient, auth: Auth, db_session: Session, container: Container
) -> None:
    document_id, _ = seeded_invoice(db_session, container)
    url = f"{API}/documents/{document_id}"
    assert client.delete(url, headers=auth(ApiRole.REVIEWER)).status_code == 403
    assert client.delete(url, headers=auth(ApiRole.ADMIN)).status_code == 204
    assert client.get(url, headers=auth(ApiRole.VIEWER)).status_code == 404
    db_session.expire_all()
    assert db_session.get_one(Document, document_id).deleted_at is not None  # soft: the row stays
