"""Manual corrections (PATCH /extractions/{id}).

Everything happens in ONE transaction: the field changes, the corrections
audit rows, field_meta (source=manual, confidence 1.0), match keys, the
lock_version bump and re-validation. Either the reviewer sees a consistent
result with fresh issues, or nothing changed at all.

Values go through the same Step 4 normalizers the pipeline uses, so a
corrected total is parsed exactly like an extracted one. Ambiguous input
("1,234", "03/04/2026") is refused rather than guessed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import (
    Conflict,
    FieldError,
    NotFound,
    PreconditionFailed,
    PreconditionRequired,
    ValidationFailed,
)
from app.api.repositories.extractions import ExtractionRepository
from app.api.schemas.results import CorrectionRequest, ExtractionOut, LineItemOp
from app.api.services import views
from app.extraction.assemble import NormContext, normalize_value
from app.extraction.normalize import invoice_number_norm, supplier_name_norm
from app.models import Correction, Document, Extraction, LineItem
from app.models.enums import FieldSource
from app.search.indexer import ChunkIndexer
from app.validation.policy import Trigger
from app.validation.repository import ValidationRepository

log = logging.getLogger(__name__)

MANUAL_CONFIDENCE: Final = 1.0
LINE_VALUE_FIELDS: Final = ("description", "quantity", "unit_price", "amount", "tax_rate")
NORMALIZER_NAME: Final = {"tax_rate": "amount"}  # a rate parses like any other decimal
MATCH_KEYS: Final[dict[str, tuple[str, Callable[[str | None], str | None]]]] = {
    "invoice_number": ("invoice_number_norm", invoice_number_norm),
    "supplier_name": ("supplier_name_norm", supplier_name_norm),
}


def parse_if_match(header: str | None) -> int:
    if header is None or not header.strip():
        raise PreconditionRequired(
            "Send If-Match with the extraction's lock_version.", code="if_match_required"
        )
    token = header.strip().removeprefix("W/").strip('"')
    if not token.isdigit():
        raise PreconditionFailed("If-Match must be the extraction's lock_version.", code="version_mismatch")
    return int(token)


@dataclass(slots=True)
class _Parser:
    """Raw strings -> typed values; collects every field error before failing."""

    ctx: NormContext
    errors: list[FieldError] = field(default_factory=list)

    def parse(self, name: str, raw: str | None, loc: tuple[str | int, ...]) -> Any:
        if raw is None or not raw.strip():
            return None
        parsed = normalize_value(NORMALIZER_NAME.get(name, name), raw, self.ctx)
        if parsed.value is None or parsed.ambiguous:
            self.errors.append(FieldError(loc, parsed.note or f"invalid value for {name}", f"invalid_{name}"))
        return parsed.value


def _manual_meta(raw: str | None, actor: str, now: datetime) -> dict[str, Any]:
    return {
        "raw_value": raw,
        "evidence_text": None,
        "unit_index": None,
        "source": FieldSource.MANUAL.value,
        "confidence": MANUAL_CONFIDENCE,
        "grounded": True,
        "corrected_by": actor,
        "corrected_at": now.isoformat(),
    }


def _line_snapshot(item: LineItem) -> dict[str, Any]:
    return {name: views.as_text(getattr(item, name)) for name in LINE_VALUE_FIELDS}


class CorrectionService:
    def __init__(self, session: Session, container: Container) -> None:
        self._session = session
        self._c = container
        self._repo = ExtractionRepository(session)

    def correct(
        self, extraction_id: uuid.UUID, request: CorrectionRequest, if_match: str | None, principal: Principal
    ) -> ExtractionOut:
        expected_version = parse_if_match(if_match)
        extraction = self._load(extraction_id)
        if extraction.lock_version != expected_version:
            raise PreconditionFailed(
                f"The extraction changed (lock_version is {extraction.lock_version}); reload and retry.",
                code="version_mismatch",
            )
        parser = _Parser(self._norm_context(request))
        header = self._parse_header(request, parser)
        lines = [(i, op, self._parse_line(i, op, parser)) for i, op in enumerate(request.line_items)]
        if parser.errors:
            raise ValidationFailed(
                "Some values could not be parsed.", code="invalid_values", errors=parser.errors
            )

        now = datetime.now(UTC)
        # Positions are never reused within a request, so "line_items[3]" in the audit trail
        # names exactly one item even when another is deleted in the same PATCH.
        next_position = max((li.position for li in extraction.line_items), default=-1) + 1
        recorder = _Recorder(self._session, extraction, principal.name, request.reason, next_position)
        meta = dict(extraction.field_meta or {})
        for name, (raw, value) in header.items():
            if getattr(extraction, name) == value:
                continue
            recorder.record(name, getattr(extraction, name), value)
            setattr(extraction, name, value)
            meta[name] = _manual_meta(raw, principal.name, now)
            if name in MATCH_KEYS:
                column, normalizer = MATCH_KEYS[name]
                setattr(extraction, column, normalizer(value))
        for index, op, values in lines:
            self._apply_line(extraction, index, op, values, recorder, now)
        if not recorder.changed:
            return self._result(extraction)
        extraction.field_meta = meta
        extraction.updated_at = now  # forces the UPDATE (and lock_version bump) for line-only edits
        try:
            self._session.flush()
            self._c.validation_service().validate_in(self._session, extraction.id, trigger=Trigger.CORRECTION)
            ValidationRepository(self._session).refresh_document_status(extraction.document_id)
            # The summary chunk mirrors the corrected fields (same transaction, so search
            # never shows the old value next to the new one).
            ChunkIndexer(self._session, self._c.search_settings).rebuild_summary(extraction.id)
            self._session.commit()
        except StaleDataError as exc:
            self._session.rollback()
            raise PreconditionFailed(
                "The extraction was changed concurrently; reload and retry.", code="version_mismatch"
            ) from exc
        self._schedule_embedding(extraction.document_id)
        return self._result(extraction)

    def _schedule_embedding(self, document_id: uuid.UUID) -> None:
        """Best effort: the beat sweep embeds pending chunks anyway."""
        try:
            self._c.enqueuer.enqueue_embedding(document_id)
        except Exception:
            log.warning(
                "could not schedule embedding", extra={"document_id": str(document_id)}, exc_info=True
            )

    # ------------------------------------------------------------ helpers

    def _load(self, extraction_id: uuid.UUID) -> Extraction:
        extraction = self._repo.get(extraction_id)
        if (
            extraction is None
            or self._session.get_one(Document, extraction.document_id).deleted_at is not None
        ):
            raise NotFound(f"Extraction {extraction_id} was not found.", code="extraction_not_found")
        if not extraction.is_current:
            raise Conflict("Only the current version of an extraction can be corrected.", code="not_current")
        return extraction

    @staticmethod
    def _norm_context(request: CorrectionRequest) -> NormContext:
        """Document-level evidence = the values in this request (all typed by one person)."""
        fields = request.fields
        amounts = [getattr(fields, n) for n in ("subtotal", "tax", "total")] if fields else []
        amounts += [getattr(op, n) for op in request.line_items for n in ("quantity", "unit_price", "amount")]
        dates = [fields.invoice_date] if fields else []
        return NormContext.build(amounts, dates, "")

    @staticmethod
    def _parse_header(request: CorrectionRequest, parser: _Parser) -> dict[str, tuple[str | None, Any]]:
        if request.fields is None:
            return {}
        return {
            name: (raw := getattr(request.fields, name), parser.parse(name, raw, ("body", "fields", name)))
            for name in sorted(request.fields.model_fields_set)
        }

    @staticmethod
    def _parse_line(index: int, op: LineItemOp, parser: _Parser) -> dict[str, tuple[str | None, Any]]:
        loc: tuple[str | int, ...] = ("body", "line_items", index)
        if op.op in ("update", "delete") and op.id is None:
            parser.errors.append(FieldError((*loc, "id"), f"id is required for {op.op}", "missing"))
        if op.op == "add" and op.id is not None:
            parser.errors.append(FieldError((*loc, "id"), "id must not be sent for add", "extra_forbidden"))
        names = [n for n in LINE_VALUE_FIELDS if n in op.model_fields_set]
        return {n: (raw := getattr(op, n), parser.parse(n, raw, (*loc, n))) for n in names}

    def _apply_line(
        self,
        extraction: Extraction,
        index: int,
        op: LineItemOp,
        values: dict[str, tuple[str | None, Any]],
        recorder: _Recorder,
        now: datetime,
    ) -> None:
        if op.op == "add":
            position = recorder.take_position()
            added = LineItem(position=position, **{n: v for n, (_, v) in values.items()})
            added.field_meta = {n: _manual_meta(raw, recorder.actor, now) for n, (raw, _) in values.items()}
            extraction.line_items.append(added)
            self._session.flush()
            recorder.record(
                f"line_items[{position}]", None, _line_snapshot(added), line_item_id=added.id, raw=True
            )
            return
        item = next((li for li in extraction.line_items if li.id == op.id), None)
        if item is None:
            raise ValidationFailed(
                "Unknown line item.",
                code="invalid_values",
                errors=[
                    FieldError(
                        ("body", "line_items", index, "id"),
                        "no such line item on this extraction",
                        "not_found",
                    )
                ],
            )
        if op.op == "delete":
            recorder.record(
                f"line_items[{item.position}]", _line_snapshot(item), None, line_item_id=item.id, raw=True
            )
            self._session.flush()  # the audit row exists before the item goes (FK SET NULL keeps it)
            extraction.line_items.remove(item)
            self._session.flush()
            return
        meta = dict(item.field_meta or {})
        for name, (raw, value) in values.items():
            if getattr(item, name) == value:
                continue
            recorder.record(
                f"line_items[{item.position}].{name}", getattr(item, name), value, line_item_id=item.id
            )
            setattr(item, name, value)
            meta[name] = _manual_meta(raw, recorder.actor, now)
        item.field_meta = meta

    def _result(self, extraction: Extraction) -> ExtractionOut:
        self._session.refresh(extraction)
        ids = [extraction.id]
        return views.extraction_out(
            extraction, self._repo.active_issues(ids), self._repo.reconciliations(ids)
        )


@dataclass(slots=True)
class _Recorder:
    """Writes corrections rows. Values are stored as strings (exact Decimals)."""

    session: Session
    extraction: Extraction
    actor: str
    reason: str | None
    next_position: int
    changed: bool = False

    def take_position(self) -> int:
        position, self.next_position = self.next_position, self.next_position + 1
        return position

    def record(
        self, path: str, old: Any, new: Any, *, line_item_id: uuid.UUID | None = None, raw: bool = False
    ) -> None:
        self.changed = True
        self.session.add(
            Correction(
                extraction_id=self.extraction.id,
                line_item_id=line_item_id,
                field_path=path,
                old_value=old if raw else views.as_text(old),
                new_value=new if raw else views.as_text(new),
                corrected_by=self.actor,
                reason=self.reason,
            )
        )
