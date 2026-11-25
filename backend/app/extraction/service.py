"""ExtractionService: one document -> versioned extractions, segment by segment.

Per segment: tabular rules (sheets) or the LLM -> normalize -> ground ->
score -> persist in its own transaction. A failing segment is persisted with
all-null fields and the error in field_meta; it never discards the others.
Job-level conditions (quota spent, every model down, bad credentials)
propagate so the orchestrator can defer, retry or fail the job.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from sqlalchemy.orm import Session

from app.config import ExtractionSettings
from app.extraction.assemble import assemble_llm
from app.extraction.draft import ExtractionDraft, FieldResult, LLMTrace
from app.extraction.inputs import PromptPart, merge_parts, messages_for, plan_parts
from app.extraction.prompts import PromptTemplate
from app.extraction.repository import DocumentInputs, ExtractionRepository, StoredUnit
from app.extraction.schemas import HEADER_FIELDS, InvoiceExtractionLLM
from app.extraction.tabular import extract_table
from app.llm import ChatResult, ExtractionParseError, StructuredChat, TokenUsage
from app.llm.clock import Clock
from app.models.enums import ErrorCode

log = logging.getLogger(__name__)

PURPOSE: Final = "invoice_extraction"
TABULAR_VERSION: Final = "tabular_v1"
MS_PER_S: Final = 1000
SessionFactory = Callable[[], Session]


@dataclass(slots=True)
class SegmentReport:
    unit_start: int
    unit_end: int
    source: str  # "rule" | "llm"
    status: str  # "succeeded" | "failed" | "already_done"
    invoice_indexes: list[int] = field(default_factory=list)
    duration_ms: int = 0
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    cached_parts: int = 0
    error: str | None = None
    document_type: str | None = None

    def to_details(self) -> dict[str, Any]:
        """processing_events.details: omit empty values, always keep invoice_indexes."""
        details = {k: v for k, v in asdict(self).items() if v not in (None, {}, [])}
        return {**details, "invoice_indexes": self.invoice_indexes}


@dataclass(slots=True)
class ExtractionReport:
    segments: list[SegmentReport] = field(default_factory=list)

    @property
    def llm_requests(self) -> int:
        return sum(s.requests for s in self.segments)


@dataclass(frozen=True, slots=True)
class _Work:
    """One unit of extraction work, planned before any quota is spent."""

    units: list[StoredUnit]
    drafts: list[ExtractionDraft] | None  # pre-computed by the tabular extractor
    invoice_indexes: list[int]
    auxiliary: bool = False  # a lookup/summary sheet next to a recognized invoice sheet


class ExtractionService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        chat: StructuredChat,
        prompt: PromptTemplate,
        settings: ExtractionSettings,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._chat = chat
        self._prompt = prompt
        self._settings = settings
        self._clock = clock

    def extract_document(
        self, document_id: uuid.UUID, job_id: uuid.UUID, *, force: bool = False
    ) -> ExtractionReport:
        """Idempotent per job: invoice indexes this job already saved are skipped
        BEFORE any LLM call, so a Celery retry spends no quota on them."""
        with self._session() as session:
            repo = ExtractionRepository(session)
            inputs = repo.load_inputs(document_id)
            done = repo.indexes_done_by_job(document_id, job_id)
        report = ExtractionReport()
        for work in self._plan(inputs):
            started = self._clock.now()
            first, last = work.units[0].text.unit_index, work.units[-1].text.unit_index
            if work.auxiliary:
                report.segments.append(SegmentReport(first, last, "rule", "auxiliary_sheet"))
                continue
            if set(work.invoice_indexes) <= done:
                report.segments.append(
                    SegmentReport(
                        first, last, "rule" if work.drafts else "llm", "already_done", work.invoice_indexes
                    )
                )
                continue
            segment = self._run(document_id, job_id, work, force)
            segment.duration_ms = int((self._clock.now() - started) * MS_PER_S)
            report.segments.append(segment)
        return report

    def _plan(self, inputs: DocumentInputs) -> list[_Work]:
        """Sheets go through the tabular extractor (pure, free); everything else
        becomes one LLM work item per segment. Invoice indexes run sequentially.

        In a workbook where some sheet IS a recognized invoice table, the other
        sheets (lookups, summaries, rates) are supporting data: they are
        skipped, not sent to the LLM. Only a workbook with no recognized sheet
        falls back to the LLM.
        """
        by_index = {u.text.unit_index: u for u in inputs.units}
        planned: list[tuple[list[StoredUnit], list[ExtractionDraft] | None]] = []
        for start, end in inputs.segments:
            units = [by_index[i] for i in range(start, end + 1) if i in by_index]
            if units:
                tabular = len(units) == 1 and units[0].is_tabular
                planned.append(
                    (units, extract_table(units[0].as_table(), self._settings) if tabular else None)
                )
        workbook_recognized = any(drafts for _, drafts in planned)

        work: list[_Work] = []
        next_index = 0
        for units, drafts in planned:
            if drafts is None and workbook_recognized and units[0].is_tabular:
                work.append(_Work(units, None, [], auxiliary=True))
                continue
            count = len(drafts) if drafts else 1
            work.append(_Work(units, drafts, list(range(next_index, next_index + count))))
            next_index += count
        return work

    def _run(self, document_id: uuid.UUID, job_id: uuid.UUID, work: _Work, force: bool) -> SegmentReport:
        first, last = work.units[0].text.unit_index, work.units[-1].text.unit_index
        if work.drafts is not None:
            self._persist(document_id, job_id, work.invoice_indexes, work.drafts, TABULAR_VERSION)
            return SegmentReport(first, last, "rule", "succeeded", work.invoice_indexes)
        try:
            draft = self._llm_draft(work.units, force)
        except ExtractionParseError as exc:
            draft = failed_draft(
                first, last, ErrorCode.LLM_ERROR, "model output failed validation", {"errors": exc.errors}
            )
        self._persist(document_id, job_id, work.invoice_indexes, [draft], self._prompt.version)
        trace = draft.trace
        return SegmentReport(
            first,
            last,
            "llm",
            "failed" if draft.error else "succeeded",
            work.invoice_indexes,
            model=trace.model_name if trace else None,
            usage=trace.usage.to_dict() if trace else {},
            requests=trace.attempts if trace else 0,
            cached_parts=trace.cached_parts if trace else 0,
            error=draft.error["code"] if draft.error else None,
            document_type=draft.document_type,
        )

    def _llm_draft(self, units: list[StoredUnit], force: bool) -> ExtractionDraft:
        texts = [u.text for u in units]
        parts = plan_parts(texts, self._settings.max_prompt_chars)
        answers: list[tuple[PromptPart, ChatResult[InvoiceExtractionLLM]]] = [
            (
                part,
                self._chat.complete_structured(
                    messages_for(part, self._prompt),
                    InvoiceExtractionLLM,
                    purpose=PURPOSE,
                    prompt_version=self._prompt.version,
                    force=force,
                ),
            )
            for part in parts
        ]
        merged = merge_parts([(part, answer.parsed) for part, answer in answers])
        return assemble_llm(merged, texts, self._settings, _trace(answers, self._prompt.version))

    def _persist(
        self,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
        indexes: list[int],
        drafts: list[ExtractionDraft],
        version: str,
    ) -> None:
        """One transaction per segment: a later failure can't roll this one back."""
        with self._session() as session:
            repo = ExtractionRepository(session)
            for invoice_index, draft in zip(indexes, drafts, strict=True):
                repo.save(document_id, job_id, invoice_index, draft, extractor_version=version)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


def _trace(answers: list[tuple[PromptPart, ChatResult[InvoiceExtractionLLM]]], version: str) -> LLMTrace:
    usage = TokenUsage()
    for _, answer in answers:
        usage += answer.usage
    return LLMTrace(
        model_name=",".join(dict.fromkeys(a.model_used for _, a in answers)),
        prompt_version=version,
        raw_responses=[
            {
                "scope": part.scope.value,
                "unit_indexes": list(part.unit_indexes),
                "output_mode": answer.output_mode.value,
                "reasoning_effort": answer.reasoning_effort,
                "repaired": answer.repaired,
                "cached": answer.cached,
                "response": answer.parsed.model_dump(mode="json"),
            }
            for part, answer in answers
        ],
        usage=usage,
        latency_ms=sum(a.latency_ms for _, a in answers),
        attempts=sum(a.attempts for _, a in answers),
        cached_parts=sum(1 for _, a in answers if a.cached),
        parts=len(answers),
    )


def failed_draft(
    unit_start: int, unit_end: int, code: ErrorCode, message: str, details: dict[str, Any]
) -> ExtractionDraft:
    """All-null draft that records why the segment could not be extracted."""
    null = {
        "raw_value": None,
        "evidence_text": None,
        "unit_index": None,
        "source": "llm",
        "grounded": None,
        "confidence": 0.0,
    }
    return ExtractionDraft(
        unit_start=unit_start,
        unit_end=unit_end,
        document_type="invoice",
        source="llm",
        fields={name: FieldResult(None, dict(null)) for name in HEADER_FIELDS},
        error={"code": code.value, "message": message, "details": details},
    )
