"""EmbeddingGateway: batching, dimension handling and space tagging."""

from __future__ import annotations

import json
import math

import httpx
import pytest
import respx

from app.config import EMBEDDING_DIM
from app.llm.embeddings import fit_dimension
from app.llm.errors import EmbeddingDimensionError, LLMUnavailableError
from tests.support.llm import (
    EMBED_OTHER,
    EMBED_PRIMARY,
    EMBED_SAME_SPACE,
    SPACE_BIG,
    SPACE_SMALL,
    GatewayKit,
    embed_body,
    sent_json,
)


def _by_model(responses: dict[str, httpx.Response | list[httpx.Response]]):
    queues = {m: (r if isinstance(r, list) else None) for m, r in responses.items()}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queued = queues.get(body["model"])
        if queued:
            return queued.pop(0)
        response = responses.get(body["model"], httpx.Response(500))
        if isinstance(response, list):
            return httpx.Response(500)
        if response.status_code == 200:
            return httpx.Response(200, json=embed_body(len(body["input"])))
        return response

    return respond


def test_inputs_are_batched_one_request_per_batch(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/embeddings").mock(side_effect=_by_model({EMBED_PRIMARY: httpx.Response(200)}))
    result = kit.gateways.embeddings.embed([f"chunk {i}" for i in range(70)], input_type="document")
    assert route.call_count == 3  # 32 + 32 + 6
    assert [len(sent_json(c)["input"]) for c in route.calls] == [32, 32, 6]
    assert len(result.vectors) == 70
    assert sent_json(route.calls[0])["dimensions"] == EMBEDDING_DIM
    assert sent_json(route.calls[0])["input_type"] == "search_document"


def test_vectors_are_truncated_and_l2_normalized(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/embeddings").mock(side_effect=_by_model({EMBED_PRIMARY: httpx.Response(200)}))
    result = kit.gateways.embeddings.embed(["a", "b"], input_type="query")
    assert result.dims == EMBEDDING_DIM
    for vector in result.vectors:
        assert len(vector) == EMBEDDING_DIM
        assert math.sqrt(sum(v * v for v in vector)) == pytest.approx(1.0)
    assert (result.space, result.is_primary_space, result.model_used) == (SPACE_BIG, True, EMBED_PRIMARY)


def test_cross_space_fallback_is_tagged(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/embeddings").mock(side_effect=_by_model({EMBED_OTHER: httpx.Response(200)}))
    result = kit.gateways.embeddings.embed(["a"], input_type="document")
    assert result.space == SPACE_SMALL and result.is_primary_space is False
    assert result.model_used == EMBED_OTHER


def test_same_space_fallback_reuses_finished_batches(kit: GatewayKit, router: respx.MockRouter) -> None:
    ok = httpx.Response(200, json=embed_body(32))
    route = router.post("/embeddings").mock(
        side_effect=_by_model(
            {
                EMBED_PRIMARY: [ok] + [httpx.Response(503)] * 3,  # batch 1 ok, batch 2 fails
                EMBED_SAME_SPACE: httpx.Response(200),
            }
        )
    )
    result = kit.gateways.embeddings.embed([f"c{i}" for i in range(40)], input_type="document")
    models = [sent_json(c)["model"] for c in route.calls]
    assert models == [EMBED_PRIMARY] * 4 + [EMBED_SAME_SPACE]  # batch 1 not re-embedded
    assert result.space == SPACE_BIG and result.model_used == f"{EMBED_PRIMARY},{EMBED_SAME_SPACE}"


def test_native_dimension_below_target_fails_fast(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/embeddings").respond(200, json=embed_body(1, dim=EMBEDDING_DIM // 2))
    with pytest.raises(EmbeddingDimensionError):
        kit.gateways.embeddings.embed(["a"], input_type="document")
    assert route.call_count == 1  # no silent fallback to another space


def test_long_inputs_are_capped_to_the_model_context(kit: GatewayKit, router: respx.MockRouter) -> None:
    route = router.post("/embeddings").mock(side_effect=_by_model({EMBED_PRIMARY: httpx.Response(200)}))
    result = kit.gateways.embeddings.embed(["x" * 100_000], input_type="document")
    limit = int(4096 * kit.settings.embed_chars_per_token)
    assert len(sent_json(route.calls[0])["input"][0]) == limit
    assert result.truncated_inputs == 1


def test_everything_down(kit: GatewayKit, router: respx.MockRouter) -> None:
    router.post("/embeddings").respond(503)
    with pytest.raises(LLMUnavailableError):
        kit.gateways.embeddings.embed(["a"], input_type="query")


def test_fit_dimension_keeps_exact_size_and_normalizes() -> None:
    vector = fit_dimension([3.0, 4.0] + [0.0] * (EMBEDDING_DIM - 2), EMBEDDING_DIM, "m")
    assert vector[:2] == [0.6, 0.8]
