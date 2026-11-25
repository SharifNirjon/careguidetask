"""Deterministic spreadsheet extractor: clean sheets cost ZERO LLM requests.

Runs before the LLM for sheet/CSV units. Headers are mapped through a
synonym table; if the required columns map (an invoice number plus a total
or line amount), rows are grouped by invoice number into one extraction per
invoice, with source="rule" and cell-level evidence. Otherwise it returns
None and the caller falls back to the LLM with the table text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.config import ExtractionSettings
from app.extraction.assemble import NormContext, normalize_value
from app.extraction.confidence import overall_confidence
from app.extraction.draft import ExtractionDraft, FieldResult, LineItemDraft
from app.extraction.schemas import HEADER_FIELDS, LINE_ITEM_FIELDS

HEADER_SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "invoice_number": (
        "invoice no",
        "invoice number",
        "invoice #",
        "invoice",
        "inv #",
        "inv no",
        "inv",
        "bill no",
        "bill number",
        "invoice id",
    ),
    "invoice_date": ("invoice date", "date", "bill date", "inv date"),
    "supplier_name": ("vendor", "supplier", "seller", "vendor name", "supplier name"),
    "customer_name": ("client", "customer", "buyer", "customer name", "client name", "bill to"),
    "description": ("description", "item", "product", "service", "details"),
    "quantity": ("qty", "quantity", "units"),
    "unit_price": ("rate", "unit price", "price", "unit cost"),
    "amount": ("amount", "line total", "line amount"),
    "tax": ("vat", "gst", "tax", "tax amount", "vat amount"),
    "subtotal": ("subtotal", "sub total", "net", "net amount"),
    "total": ("total", "grand total", "gross", "invoice total", "total amount", "amount due"),
    "currency": ("currency", "ccy", "cur"),
}
_SYNONYM_TO_FIELD: Final = {syn: name for name, syns in HEADER_SYNONYMS.items() for syn in syns}
_HEADER_NOISE: Final = re.compile(r"[^\w#]+")
_DEDUP_SUFFIX: Final = re.compile(r"_\d+$")  # tables.unique_headers adds "_2", "_3"
RULE_CONFIDENCE: Final = 1.0


@dataclass(frozen=True, slots=True)
class TableUnit:
    unit_index: int
    sheet_name: str | None
    structured_data: Mapping[str, Any]
    text: str


def normalize_header(header: str) -> str:
    cleaned = _HEADER_NOISE.sub(" ", _DEDUP_SUFFIX.sub("", header).casefold())
    return " ".join(cleaned.replace("_", " ").split())


def map_headers(headers: Sequence[str]) -> dict[str, str]:
    """field name -> original header (first matching column wins)."""
    mapping: dict[str, str] = {}
    for header in headers:
        field = _SYNONYM_TO_FIELD.get(normalize_header(header))
        if field and field not in mapping:
            mapping[field] = header
    return mapping


def is_extractable(mapping: Mapping[str, str]) -> bool:
    return "invoice_number" in mapping and ("total" in mapping or "amount" in mapping)


def _cell(row: Mapping[str, Any], header: str) -> str:
    return str(row.get(header, "")).strip()


@dataclass(frozen=True, slots=True)
class _Sheet:
    unit: TableUnit
    mapping: dict[str, str]
    ctx: NormContext
    settings: ExtractionSettings

    def evidence(self, row: Mapping[str, Any], header: str) -> str:
        return f"sheet {self.unit.sheet_name or 'csv'} row {row['_row']} col {header}"

    def field(self, name: str, row: Mapping[str, Any]) -> FieldResult:
        header = self.mapping[name]
        raw = _cell(row, header)
        meta: dict[str, Any] = {
            "raw_value": raw or None,
            "evidence_text": self.evidence(row, header),
            "unit_index": self.unit.unit_index,
            "source": "rule",
        }
        if not raw:
            return FieldResult(None, {**meta, "evidence_text": None, "grounded": None, "confidence": 0.0})
        parsed = normalize_value(name, raw, self.ctx)
        if parsed.value is None:
            return FieldResult(
                None,
                {
                    **meta,
                    "grounded": True,
                    "confidence": 0.0,
                    "rejected_value": raw,
                    "reason": "unparseable",
                    "note": parsed.note,
                },
            )
        factor = self.settings.ambiguous_parse_confidence_factor if parsed.ambiguous else 1.0
        return FieldResult(
            parsed.value,
            {
                **meta,
                "grounded": True,
                "confidence": RULE_CONFIDENCE * factor,
                **({"note": parsed.note} if parsed.note else {}),
            },
        )

    def header_field(self, name: str, rows: Sequence[Mapping[str, Any]]) -> tuple[FieldResult, str | None]:
        """The column's value for this invoice; conflicting values -> null + note."""
        if name not in self.mapping:
            return FieldResult(
                None,
                {
                    "raw_value": None,
                    "evidence_text": None,
                    "unit_index": None,
                    "source": "rule",
                    "grounded": None,
                    "confidence": 0.0,
                },
            ), None
        filled = [r for r in rows if _cell(r, self.mapping[name])]
        distinct = {_cell(r, self.mapping[name]) for r in filled}
        if len(distinct) > 1:
            result = self.field(name, filled[0])
            meta = {
                **result.meta,
                "confidence": 0.0,
                "rejected_value": sorted(distinct),
                "reason": "conflicting_rows",
            }
            return FieldResult(None, meta), f"{name}: rows disagree ({', '.join(sorted(distinct))})"
        return self.field(name, filled[0] if filled else rows[0]), None


def _group_by_invoice(rows: Sequence[Mapping[str, Any]], header: str) -> dict[str, list[Mapping[str, Any]]]:
    """Rows without a number continue the previous invoice (line-level sheets)."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    current: str | None = None
    for row in rows:
        number = _cell(row, header) or current
        if number is None:
            continue
        current = number
        groups.setdefault(number, []).append(row)
    return groups


def extract_table(unit: TableUnit, settings: ExtractionSettings) -> list[ExtractionDraft] | None:
    """Drafts for every invoice in the sheet, or None when the layout isn't recognized."""
    data = unit.structured_data
    for block in data.get("table_blocks", []):
        mapping = map_headers(block["headers"])
        if not is_extractable(mapping):
            continue
        rows = [r for r in data.get("rows", []) if r.get("_block") == block["index"]]
        groups = _group_by_invoice(rows, mapping["invoice_number"])
        if not groups:
            continue
        amount_cols = [
            mapping[n] for n in ("subtotal", "tax", "total", "unit_price", "amount") if n in mapping
        ]
        date_cols = [mapping[n] for n in ("invoice_date",) if n in mapping]
        ctx = NormContext.build(
            [_cell(r, c) for r in rows for c in amount_cols],
            [_cell(r, c) for r in rows for c in date_cols],
            unit.text,
        )
        sheet = _Sheet(unit, mapping, ctx, settings)
        return [_draft(sheet, group) for group in groups.values()]
    return None


def _draft(sheet: _Sheet, rows: Sequence[Mapping[str, Any]]) -> ExtractionDraft:
    fields: dict[str, FieldResult] = {}
    notes: list[str] = []
    for name in HEADER_FIELDS:
        result, note = sheet.header_field(name, rows)
        fields[name] = result
        if note:
            notes.append(note)
        if result.meta.get("note"):
            notes.append(f"{name}: {result.meta['note']}")
    has_lines = any(name in sheet.mapping for name in LINE_ITEM_FIELDS)
    line_items = [_line(sheet, row) for row in rows] if has_lines else []
    return ExtractionDraft(
        unit_start=sheet.unit.unit_index,
        unit_end=sheet.unit.unit_index,
        document_type="invoice",
        source="rule",
        fields=fields,
        line_items=line_items,
        uncertainties=list(dict.fromkeys(notes)),
        overall_confidence=overall_confidence(
            {n: f.confidence for n, f in fields.items()}, sheet.settings.field_weights
        ),
    )


def _line(sheet: _Sheet, row: Mapping[str, Any]) -> LineItemDraft:
    parts = {name: sheet.field(name, row) for name in LINE_ITEM_FIELDS if name in sheet.mapping}

    def value(name: str) -> Any:
        return parts[name].value if name in parts else None

    return LineItemDraft(
        description=value("description"),
        quantity=value("quantity"),
        unit_price=value("unit_price"),
        amount=value("amount"),
        meta={name: part.meta for name, part in parts.items()},
    )
