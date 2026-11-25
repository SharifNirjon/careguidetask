"""Chunker: line boundaries, overlap, oversized lines, sheet headers, summaries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import ChunkKind, UnitType
from app.search.chunking import (
    ExtractionSummaryInput,
    LineSummary,
    UnitInput,
    chunk_unit,
    estimate_tokens,
    pack_lines,
    summary_chunk,
)

CAP = 40
OVERLAP = 10


def _page(text: str) -> UnitInput:
    return UnitInput(unit_index=0, unit_type=UnitType.PDF_PAGE, text=text)


def test_token_estimate_is_conservative() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2  # 1 word * 1.3 -> 2 beats 4 chars / 4 -> 1
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("1 2 3 4 5 6 7 8 9 10") == 13  # number-heavy text: the word term wins


def test_lines_are_never_split_when_they_fit() -> None:
    lines = [f"line {i} of the invoice body" for i in range(30)]
    chunks = pack_lines(lines, max_tokens=CAP, overlap_tokens=0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert all(line in lines for line in chunk.split("\n"))
    assert [line for c in chunks for line in c.split("\n")] == lines  # no overlap: exact partition


def test_overlap_repeats_trailing_lines() -> None:
    lines = [f"row {i} text" for i in range(20)]
    chunks = pack_lines(lines, max_tokens=CAP, overlap_tokens=OVERLAP)
    for first, second in pairwise(chunks):
        a, b = first.split("\n"), second.split("\n")
        shared = next(n for n in range(len(a) - 1, 0, -1) if a[-n:] == b[:n])  # StopIteration = no overlap
        assert 1 <= shared < len(a)  # some overlap, and the chunk still advances
        assert estimate_tokens("\n".join(b[:shared])) <= OVERLAP


def test_oversized_line_is_split_at_words() -> None:
    long_line = " ".join(f"word{i}" for i in range(200))
    chunks = pack_lines(["header", long_line, "footer"], max_tokens=CAP, overlap_tokens=0)
    assert all(estimate_tokens(c) <= CAP for c in chunks)
    words = " ".join(chunks).replace("\n", " ").split()
    assert words == ["header", *long_line.split(), "footer"]


def test_giant_single_word_is_cut_by_characters() -> None:
    blob = "A" * 1000
    chunks = pack_lines([blob], max_tokens=CAP, overlap_tokens=0)
    assert "".join(chunks) == blob
    assert all(estimate_tokens(c) <= CAP for c in chunks)


def test_blank_lines_are_dropped() -> None:
    assert pack_lines(["", "  ", "a"], max_tokens=CAP, overlap_tokens=0) == ["a"]
    assert chunk_unit(_page("   "), max_tokens=CAP, overlap_tokens=0) == []


def test_sheet_chunks_repeat_the_header() -> None:
    headers = ["Invoice", "Supplier", "Total"]
    rows = [
        {"Invoice": f"INV-{i:03d}", "Supplier": "Acme Ltd", "Total": f"{i}.00", "_row": i + 2, "_block": 0}
        for i in range(40)
    ]
    unit = UnitInput(
        unit_index=0,
        unit_type=UnitType.SHEET,
        text="ignored",
        sheet_name="Ledger",
        structured_data={"rows": rows, "table_blocks": [{"index": 0, "headers": headers}]},
    )
    chunks = chunk_unit(unit, max_tokens=60, overlap_tokens=0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.content.startswith("Sheet: Ledger\nInvoice | Supplier | Total\n")
        assert chunk.token_count <= 60
    body = [line for c in chunks for line in c.content.split("\n")[2:]]
    assert body == [f"INV-{i:03d} | Acme Ltd | {i}.00" for i in range(40)]


def test_sheet_without_structure_falls_back_to_text() -> None:
    unit = UnitInput(unit_index=1, unit_type=UnitType.CSV, text="a,b\n1,2", structured_data=None)
    assert [c.content for c in chunk_unit(unit, max_tokens=CAP, overlap_tokens=0)] == ["a,b\n1,2"]


def _summary(**overrides: object) -> ExtractionSummaryInput:
    values: dict[str, object] = {
        "document_type": "invoice",
        "invoice_number": "INV-2026-001",
        "invoice_date": date(2026, 3, 1),
        "supplier_name": "Acme Ltd",
        "customer_name": "Beta Corp",
        "subtotal": Decimal("1000.0000"),
        "tax": Decimal("234.5600"),
        "total": Decimal("1234.5600"),
        "currency": "USD",
        "line_items": [LineSummary("Consulting", Decimal("1000.0000")), LineSummary(None, Decimal("1"))],
    }
    return ExtractionSummaryInput(**{**values, **overrides})  # type: ignore[arg-type]


def test_summary_chunk_content() -> None:
    chunk = summary_chunk(_summary(), unit_index=3, max_tokens=400)
    assert chunk.kind is ChunkKind.EXTRACTION_SUMMARY and chunk.unit_index == 3
    assert chunk.content == (
        "Invoice INV-2026-001 from Acme Ltd to Beta Corp, dated 2026-03-01 (March 2026), "
        "total 1,234.56 USD, subtotal 1,000.00, tax 234.56. Items: Consulting (1,000.00)."
    )


def test_summary_with_missing_fields_and_item_budget() -> None:
    sparse = summary_chunk(
        _summary(
            invoice_number=None,
            customer_name=None,
            invoice_date=None,
            total=None,
            subtotal=None,
            tax=None,
            line_items=[],
        ),
        unit_index=0,
        max_tokens=400,
    )
    assert sparse.content == "Invoice from Acme Ltd, currency USD."
    many = [LineSummary(f"Item number {i} with a long description", None) for i in range(50)]
    capped = summary_chunk(_summary(line_items=many), unit_index=0, max_tokens=60)
    assert capped.token_count <= 60 and "Items:" in capped.content


def test_receipt_document_type_is_named() -> None:
    assert summary_chunk(_summary(document_type="receipt"), 0, max_tokens=400).content.startswith(
        "Receipt INV"
    )


_line = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",), blacklist_characters="\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
    ),
    min_size=1,
    max_size=120,
).filter(lambda s: s.strip())


@settings(max_examples=150, deadline=None)
@given(lines=st.lists(_line, min_size=1, max_size=60), cap=st.integers(20, 200), overlap=st.integers(0, 40))
def test_invariants(lines: list[str], cap: int, overlap: int) -> None:
    chunks = pack_lines(lines, max_tokens=cap, overlap_tokens=overlap)
    assert all(estimate_tokens(c) <= cap for c in chunks), "a chunk exceeds the cap"
    joined = "\n".join(chunks)
    for line in lines:
        if estimate_tokens(line.rstrip()) <= cap:
            assert any(line.rstrip() in c.split("\n") for c in chunks), "a fitting line was lost or split"
        else:  # split at words / characters: every character survives, in order
            assert _is_subsequence("".join(line.split()), "".join(joined.split())), (
                "an oversized line lost text"
            )


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)
