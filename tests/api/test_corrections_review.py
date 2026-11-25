"""PATCH corrections (If-Match, whitelist, normalizers, audit, re-validation) and review."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.container import Container
from app.models import Correction, Document, Extraction
from app.models.enums import ApiRole, DocumentStatus, ReviewStatus
from tests.api.conftest import API
from tests.api.seed import mismatched_total, seeded_invoice

Auth = Callable[[ApiRole], dict[str, str]]


def _get(client: TestClient, auth: Auth, extraction_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"{API}/extractions/{extraction_id}", headers=auth(ApiRole.VIEWER))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _patch(
    client: TestClient, auth: Auth, extraction_id: uuid.UUID, body: dict[str, Any], version: int | str | None
) -> Any:
    headers = auth(ApiRole.REVIEWER)
    if version is not None:
        headers["If-Match"] = str(version)
    return client.patch(f"{API}/extractions/{extraction_id}", headers=headers, json=body)


def _codes(body: dict[str, Any], *, open_only: bool = True) -> list[str]:
    return [i["rule_code"] for i in body["issues"] if not (open_only and i["resolved"])]


@pytest.fixture
def broken(db_session: Session, container: Container) -> tuple[uuid.UUID, uuid.UUID]:
    return mismatched_total(db_session, container)


# ------------------------------------------------------------- corrections


def test_fixing_total_supersedes_mismatch(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], db_session: Session
) -> None:
    document_id, extraction_id = broken
    before = _get(client, auth, extraction_id)
    assert "TOTAL_MISMATCH" in _codes(before) and before["review_status"] == "needs_review"

    response = _patch(
        client,
        auth,
        extraction_id,
        {"fields": {"total": "270.00"}, "reason": "OCR misread"},
        before["lock_version"],
    )
    after = response.json()
    assert response.status_code == 200, response.text
    assert response.headers["etag"]
    assert after["fields"]["total"] == {
        **after["fields"]["total"],
        "value": "270.0000",
        "source": "manual",
        "confidence": 1.0,
        "raw_value": "270.00",
    }
    assert "TOTAL_MISMATCH" not in _codes(after)
    assert after["lock_version"] > before["lock_version"]
    assert after["review_status"] == "auto_approved"

    correction = db_session.query(Correction).filter_by(extraction_id=extraction_id).one()
    assert (correction.field_path, correction.old_value, correction.new_value) == (
        "total",
        "300.0000",
        "270.0000",
    )
    assert (correction.corrected_by, correction.reason) == ("reviewer-key", "OCR misread")
    db_session.expire_all()
    assert db_session.get_one(Document, document_id).status is DocumentStatus.COMPLETED


def test_match_keys_are_recomputed(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], db_session: Session
) -> None:
    _, extraction_id = broken
    version = _get(client, auth, extraction_id)["lock_version"]
    body = {"fields": {"invoice_number": "inv/2025-9", "supplier_name": "Umbrella Holdings Ltd."}}
    assert _patch(client, auth, extraction_id, body, version).status_code == 200
    extraction = db_session.get_one(Extraction, extraction_id)
    assert (extraction.invoice_number_norm, extraction.supplier_name_norm) == (
        "INV20259",
        "umbrella holdings",
    )


def test_line_item_ops(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], db_session: Session
) -> None:
    _, extraction_id = broken
    before = _get(client, auth, extraction_id)
    first, second = before["line_items"]
    body = {
        "line_items": [
            {"op": "update", "id": first["id"], "amount": "130.00"},
            {"op": "delete", "id": second["id"]},
            {
                "op": "add",
                "description": "Shipping",
                "quantity": "1",
                "unit_price": "95.00",
                "amount": "95.00",
            },
        ]
    }
    after = _patch(client, auth, extraction_id, body, before["lock_version"]).json()
    assert [(li["description"], li["amount"]) for li in after["line_items"]] == [
        (None, "130.0000"),
        ("Shipping", "95.0000"),
    ]
    assert after["line_items"][1]["fields"]["amount"]["source"] == "manual"
    assert after["lock_version"] > before["lock_version"]
    paths = sorted(c.field_path for c in db_session.query(Correction).filter_by(extraction_id=extraction_id))
    assert paths == ["line_items[0].amount", "line_items[1]", "line_items[2]"]


def test_unchanged_values_write_nothing(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], db_session: Session
) -> None:
    _, extraction_id = broken
    before = _get(client, auth, extraction_id)
    after = _patch(
        client, auth, extraction_id, {"fields": {"total": "300.00"}}, before["lock_version"]
    ).json()
    assert after["lock_version"] == before["lock_version"]
    assert db_session.query(Correction).count() == 0


def test_stale_if_match_is_412(client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]) -> None:
    _, extraction_id = broken
    version = _get(client, auth, extraction_id)["lock_version"]
    assert _patch(client, auth, extraction_id, {"fields": {"tax": "75.00"}}, version).status_code == 200
    stale = _patch(client, auth, extraction_id, {"fields": {"tax": "45.00"}}, version)
    assert stale.status_code == 412 and stale.json()["code"] == "version_mismatch"


@pytest.mark.parametrize("header", [None, ""])
def test_missing_if_match_is_428(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], header: str | None
) -> None:
    response = _patch(client, auth, broken[1], {"fields": {"tax": "1.00"}}, header)
    assert response.status_code == 428 and response.json()["code"] == "if_match_required"


@pytest.mark.parametrize(
    "body",
    [
        {"fields": {"review_status": "approved"}},
        {"fields": {"lock_version": 99}},
        {"corrected_by": "mallory"},
        {"line_items": [{"op": "add", "extraction_id": str(uuid.uuid4())}]},
    ],
)
def test_non_whitelisted_fields_are_422(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], body: dict[str, Any]
) -> None:
    response = _patch(client, auth, broken[1], body, 1)
    assert response.status_code == 422 and response.json()["code"] == "validation_error"


def test_invalid_values_have_field_errors(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, extraction_id = broken
    version = _get(client, auth, extraction_id)["lock_version"]
    body = {
        "fields": {"total": "twelve", "invoice_date": "2026-02-30", "currency": "ZZZ"},
        "line_items": [{"op": "update", "amount": "1.00"}],
    }
    response = _patch(client, auth, extraction_id, body, version)
    locs = {tuple(e["loc"]) for e in response.json()["errors"]}
    assert response.status_code == 422 and response.json()["code"] == "invalid_values"
    assert locs == {
        ("body", "fields", "total"),
        ("body", "fields", "invoice_date"),
        ("body", "fields", "currency"),
        ("body", "line_items", 0, "id"),
    }
    assert _get(client, auth, extraction_id)["lock_version"] == version  # nothing applied


def test_ambiguous_date_is_refused(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, extraction_id = broken
    version = _get(client, auth, extraction_id)["lock_version"]
    response = _patch(client, auth, extraction_id, {"fields": {"invoice_date": "03/04/2026"}}, version)
    assert response.status_code == 422


def test_clearing_a_value_with_null(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, extraction_id = broken
    version = _get(client, auth, extraction_id)["lock_version"]
    after = _patch(client, auth, extraction_id, {"fields": {"customer_name": None}}, version).json()
    assert after["fields"]["customer_name"]["value"] is None


def test_old_version_cannot_be_corrected(
    client: TestClient, auth: Auth, db_session: Session, container: Container
) -> None:
    document_id, old_id = seeded_invoice(db_session, container)
    from tests.validation.dbseed import add_invoice

    add_invoice(db_session, db_session.get_one(Document, document_id))  # version 2 becomes current
    db_session.commit()
    response = _patch(
        client, auth, old_id, {"fields": {"tax": "1.00"}}, _get(client, auth, old_id)["lock_version"]
    )
    assert response.status_code == 409 and response.json()["code"] == "not_current"
    versions = client.get(f"{API}/extractions/{old_id}/versions", headers=auth(ApiRole.VIEWER)).json()
    assert [(v["version"], v["is_current"]) for v in versions] == [(2, True), (1, False)]


# ------------------------------------------------------------------ review


def test_approve_with_open_errors_is_409_then_resolve_then_approve(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID], db_session: Session
) -> None:
    document_id, extraction_id = broken
    headers = auth(ApiRole.REVIEWER)
    blocked = client.post(f"{API}/extractions/{extraction_id}/approve", headers=headers, json={})
    assert blocked.status_code == 409 and blocked.json()["code"] == "unresolved_errors"

    for issue in _get(client, auth, extraction_id)["issues"]:
        if issue["severity"] == "error":
            resolved = client.post(
                f"{API}/issues/{issue['id']}/resolve",
                headers=headers,
                json={"note": "Credit applied off-invoice"},
            )
            assert resolved.status_code == 200
            assert resolved.json()["resolved"] is True and resolved.json()["resolved_by"] == "reviewer-key"

    approved = client.post(f"{API}/extractions/{extraction_id}/approve", headers=headers, json={"note": "ok"})
    body = approved.json()
    assert approved.status_code == 200
    assert (body["review_status"], body["reviewed_by"], body["review_note"]) == (
        "approved",
        "reviewer-key",
        "ok",
    )
    db_session.expire_all()
    assert db_session.get_one(Document, document_id).status is DocumentStatus.COMPLETED


def test_reject_requires_note(client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]) -> None:
    _, extraction_id = broken
    url = f"{API}/extractions/{extraction_id}/reject"
    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={}).status_code == 422
    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={"note": ""}).status_code == 422
    rejected = client.post(url, headers=auth(ApiRole.REVIEWER), json={"note": "Not our supplier"})
    assert rejected.status_code == 200 and rejected.json()["review_status"] == ReviewStatus.REJECTED.value


def test_reopen_restores_blocking(
    client: TestClient, auth: Auth, broken: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, extraction_id = broken
    headers = auth(ApiRole.REVIEWER)
    issue = next(i for i in _get(client, auth, extraction_id)["issues"] if i["rule_code"] == "TOTAL_MISMATCH")
    assert (
        client.post(f"{API}/issues/{issue['id']}/reopen", headers=headers).status_code == 409
    )  # not resolved
    client.post(f"{API}/issues/{issue['id']}/resolve", headers=headers, json={"note": "fine"})
    reopened = client.post(f"{API}/issues/{issue['id']}/reopen", headers=headers)
    assert reopened.status_code == 200 and reopened.json()["resolved"] is False
    assert (
        client.post(f"{API}/extractions/{extraction_id}/approve", headers=headers, json={}).status_code == 409
    )


def test_resolving_needs_note_and_unknown_issue_is_404(client: TestClient, auth: Auth) -> None:
    url = f"{API}/issues/{uuid.uuid4()}/resolve"
    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={}).status_code == 422
    assert client.post(url, headers=auth(ApiRole.REVIEWER), json={"note": "x"}).status_code == 404
