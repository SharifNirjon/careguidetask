"""Auth, errors, request ids, CORS, rate limits, health, serialization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.app import create_app
from app.api.container import Container
from app.api.services.auth import AuthService
from app.config import ApiSettings
from app.models.enums import ApiRole
from tests.api.conftest import ALLOWED_ORIGIN, API
from tests.api.seed import seeded_invoice

Auth = Callable[[ApiRole], dict[str, str]]
PROBLEM_KEYS = {"type", "title", "status", "detail", "instance", "code", "request_id", "errors"}


# ------------------------------------------------------------------ auth


def test_no_key_is_401_problem(client: TestClient) -> None:
    response = client.get(f"{API}/documents")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert set(response.json()) == PROBLEM_KEYS


@pytest.mark.parametrize("token", ["garbage", "cg_deadbeef_notarealsecret", "cg_short"])
def test_unknown_or_malformed_key_is_401(client: TestClient, token: str) -> None:
    assert client.get(f"{API}/documents", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_revoked_key_is_401(client: TestClient, keys: dict[ApiRole, str], db_session: Session) -> None:
    headers = {"Authorization": f"Bearer {keys[ApiRole.VIEWER]}"}
    assert client.get(f"{API}/documents", headers=headers).status_code == 200
    assert AuthService(db_session).revoke_key(keys[ApiRole.VIEWER])
    assert client.get(f"{API}/documents", headers=headers).status_code == 401


def test_viewer_cannot_write(client: TestClient, auth: Auth, files: dict[str, bytes]) -> None:
    upload = client.post(
        f"{API}/documents",
        headers=auth(ApiRole.VIEWER),
        files=[("files", ("a.pdf", files["invoice.pdf"], "application/pdf"))],
    )
    patch = client.patch(
        f"{API}/extractions/00000000-0000-4000-8000-000000000000",
        headers={**auth(ApiRole.VIEWER), "If-Match": "1"},
        json={},
    )
    assert (upload.status_code, patch.status_code) == (403, 403)
    assert upload.json()["code"] == "forbidden"


def test_admin_only_routes(client: TestClient, auth: Auth) -> None:
    assert client.get(f"{API}/stats", headers=auth(ApiRole.REVIEWER)).status_code == 403
    assert client.get(f"{API}/stats", headers=auth(ApiRole.ADMIN)).status_code == 200


def test_health_needs_no_auth(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok", "components": {}}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ok"


def test_ready_is_503_when_db_down(container: Container, api_settings: ApiSettings) -> None:
    def down() -> None:
        raise ConnectionRefusedError("db host 10.0.0.5 refused")  # must not leak

    broken = replace(container, health_checks={**container.health_checks, "database": down})
    response = TestClient(create_app(api_settings, broken)).get("/health/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["components"]["database"] == {"ok": False, "detail": "ConnectionRefusedError"}
    assert body["components"]["redis"]["ok"] is True
    assert "10.0.0.5" not in response.text


# ---------------------------------------------------------------- errors


def test_422_is_problem_json_with_field_errors(client: TestClient, auth: Auth) -> None:
    response = client.get(f"{API}/documents", params={"limit": 1000}, headers=auth(ApiRole.VIEWER))
    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert body["code"] == "validation_error"
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_unhandled_exception_is_generic_500(container: Container, api_settings: ApiSettings) -> None:
    app = create_app(api_settings, container)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail /etc/passwd")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/boom", headers={"X-Request-ID": "req-500"}
    )
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["request_id"] == "req-500"
    assert "secret" not in response.text and "Traceback" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_request_id_is_echoed_or_generated(client: TestClient) -> None:
    assert (
        client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    )
    generated = client.get("/health/live", headers={"X-Request-ID": "bad id\nwith newline"}).headers[
        "x-request-id"
    ]
    assert generated != "bad id\nwith newline" and len(generated) == 32


def test_request_id_appears_in_problem(client: TestClient) -> None:
    body = client.get(f"{API}/documents", headers={"X-Request-ID": "trace-me"}).json()
    assert body["request_id"] == "trace-me"


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/health/live").headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]


def test_body_size_limit_413(container: Container) -> None:
    settings = ApiSettings(environment="test", max_request_size_mb=1)
    response = TestClient(create_app(settings, container)).post(
        f"{API}/documents",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


def test_wildcard_cors_is_refused(container: Container) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        create_app(ApiSettings(cors_origins=["*"]), container)


def test_cors_allows_only_listed_origins(client: TestClient) -> None:
    def preflight(origin: str) -> dict[str, str]:
        return dict(
            client.options(
                f"{API}/documents", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
            ).headers
        )

    assert preflight(ALLOWED_ORIGIN).get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert "access-control-allow-origin" not in preflight("https://evil.example")


def test_rate_limit_429_with_retry_after(make_container: Callable[..., Container], auth: Auth) -> None:
    settings = ApiSettings(environment="test", read_rpm=2)
    client = TestClient(create_app(settings, make_container(settings)))
    codes = [client.get(f"{API}/documents", headers=auth(ApiRole.VIEWER)).status_code for _ in range(3)]
    limited = client.get(f"{API}/documents", headers=auth(ApiRole.VIEWER))
    assert codes == [200, 200, 429]
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    # a different key has its own budget
    assert client.get(f"{API}/documents", headers=auth(ApiRole.ADMIN)).status_code == 200


def test_money_is_serialized_as_strings(
    client: TestClient, auth: Auth, db_session: Session, container: Container
) -> None:
    document_id, _ = seeded_invoice(db_session, container)
    results = client.get(f"{API}/documents/{document_id}/results", headers=auth(ApiRole.VIEWER)).json()
    extraction = results["extractions"][0]
    assert extraction["fields"]["total"]["value"] == "270.0000"
    assert extraction["line_items"][0]["amount"] == "125.0000"
    listed = client.get(f"{API}/documents", headers=auth(ApiRole.VIEWER)).json()
    assert listed["items"][0]["invoices"][0]["total"] == "270.0000"
    assert listed["items"][0]["created_at"].endswith("Z")


def test_audit_fields_come_from_principal(
    client: TestClient, auth: Auth, files: dict[str, bytes], db_session: Session, container: Container
) -> None:
    upload = client.post(
        f"{API}/documents",
        headers=auth(ApiRole.REVIEWER),
        files=[("files", ("a.pdf", files["invoice.pdf"], "application/pdf"))],
        data={"uploaded_by": "mallory"},
    )
    document_id = upload.json()[0]["document_id"]
    detail = client.get(f"{API}/documents/{document_id}", headers=auth(ApiRole.VIEWER)).json()
    assert detail["uploaded_by"] == "reviewer-key"

    _, extraction_id = seeded_invoice(db_session, container, minutes=5, number="INV-AUDIT-1")
    version = client.get(f"{API}/extractions/{extraction_id}", headers=auth(ApiRole.VIEWER)).json()[
        "lock_version"
    ]
    client.patch(
        f"{API}/extractions/{extraction_id}",
        headers={**auth(ApiRole.REVIEWER), "If-Match": str(version)},
        json={"fields": {"customer_name": "Someone Else"}, "reason": "typo"},
    )
    corrections = client.get(
        f"{API}/extractions/{extraction_id}/corrections", headers=auth(ApiRole.VIEWER)
    ).json()
    assert corrections[0]["corrected_by"] == "reviewer-key"
