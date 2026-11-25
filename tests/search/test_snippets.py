"""Snippets are structured segments: markup in documents stays inert text."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.search.filters import DocumentFilters
from app.search.service import HybridSearchService
from app.search.snippets import (
    LESS_THAN,
    START,
    STOP,
    best_window,
    field_snippet,
    from_headline,
    highlight_terms,
    keyword_snippet,
    query_terms,
)
from app.search.types import SearchMode, SearchQuery, Segment
from tests.search.conftest import Corpus


def test_from_headline_turns_sentinels_into_segments() -> None:
    marked = f"Invoice {START}INV{STOP}-{START}2026{STOP} total {LESS_THAN}b>5{LESS_THAN}/b>"
    assert from_headline(marked) == (
        Segment("Invoice "),
        Segment("INV", True),
        Segment("-"),
        Segment("2026", True),
        Segment(" total <b>5</b>"),
    )
    assert from_headline(f"{START}open") == (Segment("open", True),)  # unbalanced markers are tolerated


def test_query_terms_skip_exclusions_and_operators() -> None:
    assert query_terms('"acme supplies" or INV-2026-001 -beta') == {"acme", "supplies", "inv", "2026", "001"}


def test_highlight_is_case_insensitive_whole_words() -> None:
    assert highlight_terms("ACME acmeish Acme.", {"acme"}) == (
        Segment("ACME", True),
        Segment(" acmeish "),
        Segment("Acme", True),
        Segment("."),
    )


def test_best_window_picks_the_matching_region() -> None:
    content = " ".join(["filler"] * 100 + ["blue", "widgets", "here"] + ["filler"] * 100)
    window = best_window(content, "blue widgets", words=10, stride=5)
    assert "blue widgets" in window and window.startswith("… ") and window.endswith(" …")


def test_keyword_snippet_falls_back_to_python_highlighting() -> None:
    segments = keyword_snippet(
        "Invoice INV-2026-001 consulting", "unused", "001"
    )  # ts_headline marked nothing
    assert [s.text for s in segments if s.highlight] == ["001"]


def test_field_snippet() -> None:
    assert field_snippet("Supplier", "Acme Supplies Ltd", "acme") == (
        Segment("Supplier: "),
        Segment("Acme", True),
        Segment(" Supplies Ltd"),
    )


def test_script_content_comes_back_as_plain_text(
    corpus: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    corpus.add("xss", ['Note <script>alert("x")</script> <img src=x onerror=alert(1)> payment terms'])
    with session_factory() as session:
        result = make_service().search(
            session, SearchQuery("payment", SearchMode.KEYWORD, DocumentFilters(), 5)
        )
    snippet = result.hits[0].snippet
    text = "".join(s.text for s in snippet)
    assert '<script>alert("x")</script>' in text and "<img src=x onerror=alert(1)>" in text  # verbatim, inert
    assert [s.text for s in snippet if s.highlight] == ["payment"]
    assert all(isinstance(s, Segment) for s in snippet)
