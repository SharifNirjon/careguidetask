from app.models.enums import UnitType
from app.pipeline.segment import HeuristicSegmenter, segment_document
from tests.fixtures.factory import invoice_lines


def _page(number: str, page: int, pages: int, *, totals: bool) -> str:
    return "\n".join(invoice_lines(number, page, pages, totals=totals))


def test_three_invoices_across_seven_pages() -> None:
    texts = [
        _page("INV-A1", 1, 3, totals=False),
        _page("INV-A1", 2, 3, totals=False),
        _page("INV-A1", 3, 3, totals=True),
        _page("INV-B2", 1, 1, totals=True),
        _page("INV-C3", 1, 3, totals=False),
        _page("INV-C3", 2, 3, totals=False),
        _page("INV-C3", 3, 3, totals=True),
    ]
    segments = HeuristicSegmenter().segment(texts)
    assert [(s.unit_start, s.unit_end) for s in segments] == [(0, 2), (3, 3), (4, 6)]
    assert [s.invoice_number for s in segments] == ["INV-A1", "INV-B2", "INV-C3"]
    assert all(s.confidence >= 0.8 for s in segments)
    assert "page_counter_reset" in segments[1].reasons


def test_repeated_header_on_continuation_page_is_not_a_boundary() -> None:
    """Same invoice number + repeated INVOICE title + 'Total' carried forward."""
    texts = [
        "INVOICE\nInvoice No: X-100\nSubtotal carried forward 50.00",
        "INVOICE\nInvoice No: X-100\nTotal 90.00",
    ]
    assert [(s.unit_start, s.unit_end) for s in HeuristicSegmenter().segment(texts)] == [(0, 1)]


def test_invoice_number_change_without_counters() -> None:
    texts = ["Invoice No: 111\nTotal 1", "Invoice No: 222\nTotal 2"]
    segments = HeuristicSegmenter().segment(texts)
    assert [(s.unit_start, s.unit_end) for s in segments] == [(0, 0), (1, 1)]


def test_no_signals_is_one_low_confidence_segment() -> None:
    segments = HeuristicSegmenter().segment(["lorem ipsum", "dolor sit amet", ""])
    assert len(segments) == 1 and (segments[0].unit_start, segments[0].unit_end) == (0, 2)
    assert segments[0].confidence < 0.5


def test_spreadsheets_get_one_segment_per_sheet() -> None:
    segments = segment_document(UnitType.SHEET, ["a", "b"])
    assert [(s.unit_start, s.unit_end) for s in segments] == [(0, 0), (1, 1)]


def test_bare_page_counter() -> None:
    texts = ["Bill\nRef A\n1/2", "items\n2/2\nBalance Due 5", "Bill\n1/1\nTotal 9"]
    assert [(s.unit_start, s.unit_end) for s in HeuristicSegmenter().segment(texts)] == [(0, 1), (2, 2)]
