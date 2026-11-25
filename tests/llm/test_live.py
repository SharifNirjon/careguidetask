"""Live OpenRouter checks. Spend real free-tier quota (2 requests): opt in with
RUN_LIVE_LLM=1 and a real OPENROUTER_API_KEY. Synthetic text only."""

from __future__ import annotations

import math

import pytest
from pydantic import BaseModel
from redis import Redis

from app.config import EMBEDDING_DIM, get_llm_settings
from app.llm import ChatMessage
from app.llm.factory import build_gateways

pytestmark = pytest.mark.live


class Echo(BaseModel):
    invoice_number: str | None


def _gateways():
    settings = get_llm_settings()
    if not settings.openrouter_api_key.get_secret_value():
        pytest.skip("OPENROUTER_API_KEY not set")
    return build_gateways(settings, Redis.from_url(settings.redis_url))


def test_live_structured_chat() -> None:
    result = _gateways().chat.complete_structured(
        [ChatMessage("user", 'Extract the invoice number from: "INVOICE No. SYN-4242". Return JSON.')],
        Echo,
        purpose="live_test",
        prompt_version="live_v1",
        force=True,
    )
    assert result.parsed.invoice_number == "SYN-4242"
    assert result.latency_ms > 0 and result.model_used


def test_live_embedding_dimension_and_norm() -> None:
    result = _gateways().embeddings.embed(["synthetic invoice text"], input_type="document")
    assert result.dims == EMBEDDING_DIM and len(result.vectors[0]) == EMBEDDING_DIM
    assert math.sqrt(sum(v * v for v in result.vectors[0])) == pytest.approx(1.0, abs=1e-6)
