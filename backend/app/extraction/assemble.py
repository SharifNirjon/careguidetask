"""LLM result -> ExtractionDraft: ground, normalize, score. Pure.

Order per field: empty? -> null. Grounded? no -> null + rejected_value.
Parseable? no -> null + rejected_value. Otherwise typed value with fused
confidence. Nothing the model said is lost: it stays in field_meta.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.config import ExtractionSettings
from app.extraction.confidence import field_confidence, overall_confidence
from app.extraction.draft import ExtractionDraft, FieldResult, FieldValue, LineItemDraft, LLMTrace
from app.extraction.grounding import ground
from app.extraction.inputs import UnitText
from app.extraction.normalize import (
    Parsed,
    find_amount_strings,
    find_date_strings,
    infer_day_first,
    infer_decimal_separator,
    normalize_currency,
    parse_amount,
    parse_date,
)
from app.extraction.schemas import HEADER_FIELDS, LINE_ITEM_FIELDS, ExtractedField, InvoiceExtractionLLM
from app.pipeline.text_utils import clean_text

AMOUNT_FIELDS: Final = frozenset({"subtotal", "tax", "total", "quantity", "unit_price", "amount"})
TEXT_FIELDS: Final = frozenset({"invoice_number", "supplier_name", "customer_name", "description"})
MAX_TEXT_VALUE_CHARS: Final = 500
NOT_AN_INVOICE_NOTE: Final = "NOT_AN_INVOICE"


@dataclass(frozen=True, slots=True)
class NormContext:
    """Document-level evidence that settles ambiguous formats."""

    decimal_separator: str | None
    day_first: bool | None
    document_text: str
    amount_raws: tuple[str, ...]

    @classmethod
    def build(cls, amounts: Iterable[str | None], dates: Iterable[str | None], text: str) -> NormContext:
        """Evidence = the extracted values plus every date/amount printed in the segment."""
        amount_list = tuple(a for a in amounts if a)
        return cls(
            decimal_separator=infer_decimal_separator([*amount_list, *find_amount_strings(text)]),
            day_first=infer_day_first([*dates, *find_date_strings(text)]),
            document_text=text,
            amount_raws=amount_list,
        )


def normalize_value(name: str, raw: str, ctx: NormContext) -> Parsed[Any]:
    """Typed value for one field name (shared by the LLM and tabular paths)."""
    if name in AMOUNT_FIELDS:
        return parse_amount(raw, ctx.decimal_separator)
    if name == "invoice_date":
        return parse_date(raw, day_first=ctx.day_first)
    if name == "currency":
        return normalize_currency(raw, document_text=ctx.document_text, amount_raws=ctx.amount_raws)
    text = clean_text(raw, MAX_TEXT_VALUE_CHARS).text
    return Parsed(text or None)


def _meta(field: ExtractedField, **extra: Any) -> dict[str, Any]:
    return {
        "raw_value": field.raw_value,
        "evidence_text": field.evidence,
        "unit_index": field.unit_index,
        "source": "llm",
        "llm_confidence": field.confidence,
        **extra,
    }


@dataclass(frozen=True, slots=True)
class _Assembler:
    units: dict[int, UnitText]
    ctx: NormContext
    settings: ExtractionSettings
    note: Callable[[str], None]

    def field(self, name: str, field: ExtractedField) -> FieldResult:
        if field.is_empty:
            return FieldResult(None, _meta(field, grounded=None, confidence=0.0))
        grounding = ground(
            field, {i: u.text for i, u in self.units.items()}, min_score=self.settings.grounding_min_score
        )
        if not grounding.grounded:
            return FieldResult(
                None,
                _meta(
                    field,
                    grounded=False,
                    confidence=0.0,
                    rejected_value=field.raw_value,
                    reason="ungrounded",
                    grounding_failure=grounding.failure.value if grounding.failure else None,
                    grounding_score=round(grounding.score, 3),
                ),
            )
        raw = field.raw_value or ""  # non-empty: checked by is_empty above
        parsed = normalize_value(name, raw, self.ctx)
        if parsed.note:
            self.note(f"{name}: {parsed.note}")
        if parsed.value is None:
            return FieldResult(
                None,
                _meta(
                    field,
                    grounded=True,
                    confidence=0.0,
                    rejected_value=field.raw_value,
                    reason="unparseable",
                    note=parsed.note,
                ),
            )
        unit = self.units.get(field.unit_index) if field.unit_index is not None else None
        confidence = field_confidence(
            field.confidence,
            grounding.score,
            unit.ocr_confidence if unit else None,
            ambiguity_factor=self.settings.ambiguous_parse_confidence_factor if parsed.ambiguous else None,
        )
        return FieldResult(
            parsed.value,
            _meta(field, grounded=True, confidence=confidence, grounding_score=round(grounding.score, 3)),
        )


def _currency_from_amounts(fields: dict[str, FieldResult], ctx: NormContext) -> FieldResult | None:
    """No currency field printed, but a grounded amount carries a symbol/code ('€ 1.234,00')."""
    grounded = [fields[n] for n in ("total", "subtotal", "tax") if fields[n].value is not None]
    raws = [f.meta["raw_value"] for f in grounded]
    parsed = normalize_currency(None, document_text=ctx.document_text, amount_raws=raws)
    if parsed.value is None:
        return None
    source = grounded[0].meta
    return FieldResult(
        parsed.value,
        {
            "raw_value": None,
            "evidence_text": source["evidence_text"],
            "unit_index": source["unit_index"],
            "source": "rule",
            "derived_from": "amount_symbol",
            "grounded": True,
            "confidence": min(f.confidence for f in grounded),
        },
    )


def assemble_llm(
    result: InvoiceExtractionLLM,
    units: list[UnitText],
    settings: ExtractionSettings,
    trace: LLMTrace,
) -> ExtractionDraft:
    """Ground + normalize + score one segment's LLM answer."""
    amounts = [result.header(n).raw_value for n in ("subtotal", "tax", "total")]
    amounts += [getattr(item, n).raw_value for item in result.line_items for n in ("unit_price", "amount")]
    ctx = NormContext.build(amounts, [result.invoice_date.raw_value], "\n".join(u.text for u in units))
    uncertainties = list(result.uncertainties)
    assembler = _Assembler({u.unit_index: u for u in units}, ctx, settings, uncertainties.append)

    fields = {name: assembler.field(name, result.header(name)) for name in HEADER_FIELDS}
    if result.currency.is_empty and (derived := _currency_from_amounts(fields, ctx)) is not None:
        fields["currency"] = derived
    line_items = []
    for item in result.line_items:
        parts = {name: assembler.field(name, getattr(item, name)) for name in LINE_ITEM_FIELDS}
        line_items.append(
            LineItemDraft(
                description=_text(parts["description"].value),
                quantity=_amount(parts["quantity"].value),
                unit_price=_amount(parts["unit_price"].value),
                amount=_amount(parts["amount"].value),
                meta={name: part.meta for name, part in parts.items()},
            )
        )
    if result.document_type == "other":
        uncertainties.append(NOT_AN_INVOICE_NOTE)
    return ExtractionDraft(
        unit_start=min(u.unit_index for u in units),
        unit_end=max(u.unit_index for u in units),
        document_type=result.document_type,
        source="llm",
        fields=fields,
        line_items=line_items,
        uncertainties=list(dict.fromkeys(uncertainties)),
        overall_confidence=overall_confidence(
            {n: f.confidence for n, f in fields.items()}, settings.field_weights
        ),
        trace=trace,
    )


def _text(value: FieldValue) -> str | None:
    return value if isinstance(value, str) else None


def _amount(value: FieldValue) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
