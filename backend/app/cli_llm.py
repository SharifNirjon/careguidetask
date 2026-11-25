"""CLI commands for the LLM layer: quota/probe check and per-document extraction.

python -m app.cli llm-check [--probe]
python -m app.cli extract <document_id> [--force]
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

from pydantic import BaseModel
from redis import Redis
from sqlalchemy import select

from app.config import LLMSettings, get_llm_settings
from app.extraction.schemas import HEADER_FIELDS
from app.llm import ChatMessage, LLMError
from app.llm.embeddings import NATIVE_DIM_KEY
from app.llm.factory import Gateways, build_gateways
from app.models import Extraction, ProcessingJob
from app.models.enums import JobType
from app.pipeline import orchestrator
from app.pipeline.errors import DeferredError, TransientError

EVIDENCE_PREVIEW_CHARS = 60
Printer = Callable[[Sequence[str], list[list[str]]], None]


class _Probe(BaseModel):
    ok: bool


def _gateways(settings: LLMSettings) -> tuple[Gateways, Redis]:
    redis = Redis.from_url(settings.redis_url)
    return build_gateways(settings, redis), redis


def llm_check(*, probe: bool, print_rows: Printer) -> int:
    settings = get_llm_settings()
    gateways, redis = _gateways(settings)
    remote = gateways.quota.resync()
    status = remote or gateways.quota.status()
    print_rows(
        ["quota", "used", "limit", "remaining", "source"],
        [
            [
                "free models / UTC day",
                str(status.used),
                str(status.limit),
                str(status.remaining),
                "openrouter /key" if remote else "local counter",
            ]
        ],
    )
    rows = []
    for model in settings.chat_models:
        info = gateways.catalog.get(model)
        params = sorted(info.supported_parameters) if info else ["(not in catalog)"]
        rows.append(
            [
                model,
                "chat",
                ", ".join(
                    p for p in params if p in {"structured_outputs", "response_format", "reasoning", "tools"}
                ),
            ]
        )
    rows += [[c.model, f"embed ({c.space})", ""] for c in settings.embedding_models]
    print_rows(["model", "kind", "capabilities"], rows)
    if not probe:
        return 0
    return _probe(settings, redis, print_rows)


def _probe(settings: LLMSettings, redis: Redis, print_rows: Printer) -> int:
    """One tiny request per configured model (each one costs daily quota)."""
    prompt = resources.files("app.llm.prompts").joinpath("probe.md").read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for model in settings.chat_models:
        single = settings.model_copy(update={"llm_primary_model": model, "llm_fallback_models": []})
        gateways = build_gateways(single, redis)
        rows.append([model, *_timed(lambda g=gateways: _chat_probe(g, prompt))])
    for config in settings.embedding_models:
        single = settings.model_copy(update={"embedding_models": [config]})
        gateways = build_gateways(single, redis)
        rows.append([config.model, *_timed(lambda g=gateways, m=config.model: _embed_probe(g, redis, m))])
    print_rows(["model", "result", "latency_ms"], rows)
    return 0 if all(r[1].startswith("ok") for r in rows) else 1


def _chat_probe(gateways: Gateways, prompt: str) -> str:
    result = gateways.chat.complete_structured(
        [ChatMessage("user", prompt)], _Probe, purpose="probe", prompt_version="probe", force=True
    )
    return f"ok ({result.output_mode.value}, effort={result.reasoning_effort}, attempts={result.attempts})"


def _embed_probe(gateways: Gateways, redis: Redis, model: str) -> str:
    result = gateways.embeddings.embed(["probe"], input_type="query")
    native = redis.hget(NATIVE_DIM_KEY, model)
    native_dim = native.decode() if isinstance(native, bytes) else native
    return f"ok (native dim {native_dim}, stored {result.dims}, space {result.space})"


def _timed(call: Callable[[], str]) -> list[str]:
    started = time.perf_counter()
    try:
        outcome = call()
    except LLMError as exc:
        outcome = f"FAILED: {type(exc).__name__}: {exc}"
    return [outcome, str(int((time.perf_counter() - started) * 1000))]


def extract(document_id: uuid.UUID, *, force: bool, print_rows: Printer) -> int:
    ctx = orchestrator.default_context()
    with ctx.session() as session:
        job = ProcessingJob(document_id=document_id, job_type=JobType.REEXTRACT, triggered_by="cli")
        session.add(job)
        session.flush()
        job_id = job.id
    try:
        status = orchestrator.run_llm_stages(ctx, document_id, job_id, force=force).value
    except DeferredError as exc:
        status = f"deferred until {exc.resume_at.isoformat()}"
    except TransientError as exc:
        status = f"unavailable ({exc.code.value}); retry later"
    print_rows(["document", "job", "status"], [[str(document_id), str(job_id), status]])
    with ctx.session() as session:
        current = session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document_id, Extraction.is_current.is_(True))
            .order_by(Extraction.invoice_index)
        ).all()
        for extraction in current:
            print_rows(
                ["invoice", "field", "value", "confidence", "grounded", "evidence"], _field_rows(extraction)
            )
    return 0


def _field_rows(extraction: Extraction) -> list[list[str]]:
    rows = []
    for name in HEADER_FIELDS:
        meta: dict[str, Any] = extraction.field_meta.get(name, {})
        value = getattr(extraction, name)
        shown = "-" if value is None else str(value)
        if meta.get("rejected_value") is not None:
            shown = f"- (rejected {meta['rejected_value']!r}: {meta.get('reason')})"
        evidence = (meta.get("evidence_text") or "")[:EVIDENCE_PREVIEW_CHARS]
        rows.append(
            [
                f"#{extraction.invoice_index} v{extraction.version}",
                name,
                shown,
                f"{meta.get('confidence', 0):.2f}",
                str(meta.get("grounded")),
                evidence,
            ]
        )
    return rows
