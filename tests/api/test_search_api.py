"""GET /search through the API: modes, filters, segments, degradation, paging, limits."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.app import create_app
from app.api.container import Container
from app.config import ApiSettings
from app.llm.errors import LLMUnavailableError
from app.models.enums import ApiRole
from app.search.embedding import ChunkEmbedder, EmbedBudget
from tests.api.conftest import API
from tests.api.seed import mismatched_total, seeded_invoice
from tests.search.fakes import FakeEmbeddingClient, StaticQuota

Auth = Callable[[ApiRole], dict[str, str]]


@pytest.fixture
def searchable(db_session: Session, container: Container) -> dict[str, uuid.UUID]:
    acme, _ = seeded_invoice(
        db_session,
        container,
        minutes=1,
        number="INV-2024-001",
        page_text="Invoice INV-2024-001 consulting services for March",
    )
    globex, _ = seeded_invoice(
        db_session,
        container,
        minutes=2,
        number="GX-77",
        supplier="Globex <b>Corp</b>",
        currency="USD",
        page_text="Globex <script>alert(1)</script> delivery note",
    )
    broken, _ = mismatched_total(
        db_session,
        container,
        minutes=3,
        number="INV-2024-999",
        supplier="Acme Supplies",
        page_text="Invoice INV-2024-999 office chairs and desks",
    )
    return {"acme": acme, "globex": globex, "broken": broken}


def _search(client: TestClient, auth: Auth, **params: Any) -> dict[str, Any]:
    response = client.get(f"{API}/search", params=params, headers=auth(ApiRole.VIEWER))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _text(hit: dict[str, Any]) -> str:
    return "".join(s["text"] for s in hit["snippet_segments"])


def _marked(hit: dict[str, Any]) -> list[str]:
    return [s["text"] for s in hit["snippet_segments"] if s["highlight"]]


def test_exact_invoice_number_ranks_first(
    client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]
) -> None:
    body = _search(client, auth, q="INV-2024-001", mode="keyword")
    top = body["items"][0]
    assert top["document_id"] == str(searchable["acme"])
    assert top["score"] == 1.0 and top["match_type"] == "keyword"
    assert top["total"] == "270.0000" and top["unit_index"] == 0
    assert body["degraded"] is False and body["mode"] == "keyword"


def test_partial_code_finds_both(client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]) -> None:
    ids = {h["document_id"] for h in _search(client, auth, q="INV-2024", mode="keyword")["items"]}
    assert {str(searchable["acme"]), str(searchable["broken"])} <= ids


def test_snippets_are_segments_not_html(
    client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]
) -> None:
    hit = next(
        h
        for h in _search(client, auth, q="delivery", mode="keyword")["items"]
        if h["document_id"] == str(searchable["globex"])
    )
    assert "<script>alert(1)</script>" in _text(hit)  # plain text, verbatim: the client renders it as text
    assert _marked(hit) == ["delivery"]
    assert all(set(s) == {"text", "highlight"} for s in hit["snippet_segments"])


def test_filters_apply_to_search(client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]) -> None:
    hits = _search(
        client, auth, q="INV-2024", mode="keyword", has_open_issues="true", rule_code="TOTAL_MISMATCH"
    )["items"]
    assert [h["document_id"] for h in hits] == [str(searchable["broken"])]
    assert _search(client, auth, q="INV-2024", mode="keyword", currency="USD")["items"] == []


def test_hybrid_without_embeddings_degrades(
    client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]
) -> None:
    body = _search(client, auth, q="office chairs")  # hybrid is the default; nothing is embedded yet
    assert body["mode"] == "hybrid" and body["degraded"] is True
    assert "keyword" in body["degraded_reason"]
    assert body["items"][0]["document_id"] == str(searchable["broken"])


def _embed_all(container: Container, client_: FakeEmbeddingClient) -> None:
    budget = EmbedBudget(container.redis, StaticQuota(1000), share=1.0)
    ChunkEmbedder(
        session_factory=container.session_factory, client=client_, budget=budget, batch_size=16
    ).embed_pending(limit=1000)


def test_semantic_and_hybrid_after_embedding(
    client: TestClient,
    auth: Auth,
    searchable: dict[str, uuid.UUID],
    container: Container,
    embedding_client: FakeEmbeddingClient,
) -> None:
    _embed_all(container, embedding_client)
    semantic = _search(client, auth, q="desks chairs office", mode="semantic")
    assert semantic["degraded"] is False
    assert semantic["items"][0]["document_id"] == str(searchable["broken"])
    assert semantic["items"][0]["match_type"] == "semantic"
    hybrid = _search(client, auth, q="office chairs")
    top = hybrid["items"][0]
    assert hybrid["degraded"] is False and top["document_id"] == str(searchable["broken"])
    assert top["match_type"] == "both"


def test_embedding_outage_degrades_to_keyword(
    client: TestClient,
    auth: Auth,
    searchable: dict[str, uuid.UUID],
    container: Container,
    embedding_client: FakeEmbeddingClient,
) -> None:
    _embed_all(container, embedding_client)
    embedding_client.fail_with = LLMUnavailableError({"all": "down"})
    for mode in ("semantic", "hybrid"):
        body = _search(client, auth, q="office chairs", mode=mode)
        assert body["degraded"] is True and "LLMUnavailableError" in body["degraded_reason"]
        assert body["items"][0]["document_id"] == str(searchable["broken"])
        assert all(h["match_type"] == "keyword" for h in body["items"])


def test_offset_pagination_and_cap(client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID]) -> None:
    full = _search(client, auth, q="invoice", mode="keyword", limit=10)
    first = _search(client, auth, q="invoice", mode="keyword", limit=1)
    second = _search(client, auth, q="invoice", mode="keyword", limit=1, offset=1)
    assert first["total"] == full["total"] >= 2
    assert [first["items"][0]["document_id"], second["items"][0]["document_id"]] == [
        h["document_id"] for h in full["items"][:2]
    ]
    capped = client.get(
        f"{API}/search", params={"q": "x", "offset": 150, "limit": 100}, headers=auth(ApiRole.VIEWER)
    )
    assert capped.status_code == 422 and capped.json()["code"] == "offset_out_of_range"


def test_query_bounds(client: TestClient, auth: Auth) -> None:
    headers = auth(ApiRole.VIEWER)
    assert client.get(f"{API}/search", params={"q": "x" * 501}, headers=headers).status_code == 422
    assert client.get(f"{API}/search", params={"q": ""}, headers=headers).status_code == 422


def test_search_has_its_own_stricter_rate_limit(make_container: Callable[..., Container], auth: Auth) -> None:
    settings = ApiSettings(environment="test", search_rpm=2, read_rpm=100)
    client = TestClient(create_app(settings, make_container(settings)))
    codes = [
        client.get(
            f"{API}/search", params={"q": "x", "mode": "keyword"}, headers=auth(ApiRole.VIEWER)
        ).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]
    assert client.get(f"{API}/documents", headers=auth(ApiRole.VIEWER)).status_code == 200  # reads unaffected


def test_correction_regenerates_summary_chunk(
    client: TestClient, auth: Auth, searchable: dict[str, uuid.UUID], db_session: Session, enqueuer: Any
) -> None:
    document_id = searchable["acme"]
    results = client.get(f"{API}/documents/{document_id}/results", headers=auth(ApiRole.VIEWER)).json()
    extraction = results["extractions"][0]
    assert _search(client, auth, q="Wonka", mode="keyword")["items"] == []
    response = client.patch(
        f"{API}/extractions/{extraction['id']}",
        headers={**auth(ApiRole.REVIEWER), "If-Match": str(extraction["lock_version"])},
        json={"fields": {"customer_name": "Wonka Industries"}},
    )
    assert response.status_code == 200
    hits = _search(client, auth, q="Wonka", mode="keyword")["items"]
    assert [h["document_id"] for h in hits] == [str(document_id)]
    assert enqueuer.embeddings == [document_id]  # the new summary chunk is queued for embedding
