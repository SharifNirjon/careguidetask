"""Keyword search: websearch syntax, invoice-code tokenization, field matches, boost."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models.search import TSV_EXPRESSION
from app.search.filters import DocumentFilters
from app.search.keyword import KeywordRetriever, looks_like_invoice_number, prepare_websearch, split_codes
from app.search.service import HybridSearchService
from app.search.types import SearchMode, SearchQuery
from tests.search.conftest import Corpus

ANY = DocumentFilters()


@pytest.mark.parametrize(
    ("query", "prepared"),
    [
        ("INV-2026-001", '"INV 2026 001"'),
        ("-INV-2026-001", '-"INV 2026 001"'),
        ('"acme supplies" or globex -beta', '"acme supplies" or globex -beta'),
        ('"invoice INV-9"', '"invoice INV 9"'),
        ("2026/03/01", '"2026 03 01"'),
        ("plain words", "plain words"),
        ('"unclosed', '"unclosed"'),
        ("a - b", "a - b"),
        ('x -"INV-9 a"', 'x -"INV 9 a"'),
    ],
)
def test_prepare_websearch(query: str, prepared: str) -> None:
    assert prepare_websearch(query) == prepared


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("INV-2026-001", True),
        ("20260001", True),
        ("inv2026001", True),
        ("acme", False),
        ("acme invoices march", False),
        ("12", False),
        ('"INV-7"', True),
    ],
)
def test_looks_like_invoice_number(query: str, expected: bool) -> None:
    assert looks_like_invoice_number(query) is expected


def test_python_and_sql_split_codes_identically(session_factory: sessionmaker[Session]) -> None:
    """chunks.tsv (SQL regexp) and queries (Python regex) must tokenize codes the same way."""
    sample = "INV-2026-001 a/b/c e-mail x-y 2026-03-01 -neg word- _x_ ünï-côdé"
    expression = TSV_EXPRESSION.replace("content", ":c")
    with session_factory() as session:
        from_db = session.scalar(text(f"SELECT {expression}"), {"c": sample})
        from_python = session.scalar(
            select(func.to_tsvector(text("'simple'::regconfig"), split_codes(sample)))
        )
    assert from_db == from_python


def _docs(session_factory: sessionmaker[Session], corpus: Corpus, query: str) -> list[str]:
    names = {v: k for k, v in corpus.documents.items()}
    with session_factory() as session:
        hits = KeywordRetriever(min_similarity=0.3).fulltext(session, query, ANY, 50)
    return list(dict.fromkeys(names[h.document_id] for h in hits))


@pytest.fixture
def texts(corpus: Corpus) -> Corpus:
    corpus.add("code", ["Invoice INV-2026-001 for consulting services"], number="INV-2026-001")
    corpus.add("other", ["Invoice INV-2026-002 for office supplies from Globex"], number="INV-2026-002")
    corpus.add("acme", ["Acme Supplies delivered blue widgets"], supplier="Acme Supplies Ltd")
    corpus.add("beta", ["Beta Corp paid the widget invoice late"], customer="Beta Corp")
    return corpus


def test_invoice_code_found_by_full_code_and_parts(
    texts: Corpus, session_factory: sessionmaker[Session]
) -> None:
    assert _docs(session_factory, texts, "INV-2026-001") == ["code"]
    assert _docs(session_factory, texts, "2026-001") == ["code"]
    assert set(_docs(session_factory, texts, "INV-2026")) == {"code", "other"}
    assert _docs(session_factory, texts, "001") == ["code"]


def test_phrases_or_and_exclusion(texts: Corpus, session_factory: sessionmaker[Session]) -> None:
    assert _docs(session_factory, texts, '"blue widgets"') == ["acme"]
    assert _docs(session_factory, texts, '"widgets blue"') == []
    assert set(_docs(session_factory, texts, "globex or beta")) == {"other", "beta"}
    # exclusion drops whole DOCUMENTS (every summary chunk says "Invoice ...")
    assert set(_docs(session_factory, texts, "invoice -consulting")) == {"other", "beta", "acme"}
    assert set(_docs(session_factory, texts, "invoice -INV-2026-001")) == {"other", "beta", "acme"}
    assert set(_docs(session_factory, texts, 'invoice -"blue widgets"')) == {"code", "other", "beta"}


def test_no_stemming_by_design(texts: Corpus, session_factory: sessionmaker[Session]) -> None:
    """'simple' config: 'widget' != 'widgets' in full text (semantic search covers it)."""
    assert _docs(session_factory, texts, "widget") == ["beta"]


def test_field_matches(texts: Corpus, session_factory: sessionmaker[Session]) -> None:
    retriever = KeywordRetriever(min_similarity=0.3)
    with session_factory() as session:
        exact = retriever.fields(session, "inv2026001", ANY, 10)
        supplier = retriever.fields(session, "acme suplies", ANY, 10)
        customer = retriever.fields(session, "beta", ANY, 10)
    assert exact[0].extraction_id == texts.extractions["code"] and exact[0].exact_number
    assert (
        supplier[0].extraction_id == texts.extractions["acme"]
        and supplier[0].matched_field == "supplier_name"
    )
    assert (
        customer[0].extraction_id == texts.extractions["beta"]
        and customer[0].matched_field == "customer_name"
    )


def test_exact_invoice_number_is_boosted_to_the_top(
    corpus: Corpus, session_factory: sessionmaker[Session], make_service: Callable[..., HybridSearchService]
) -> None:
    # Many chunks mention the code; only one invoice actually IS that number.
    for i in range(5):
        corpus.add(
            f"mention{i}", [f"Payment reference INV-2026-500 INV-2026-500 credit {i}"], number=f"P-{i}"
        )
    corpus.add("target", ["Statement"], number="INV-2026-500")
    with session_factory() as session:
        result = make_service().search(session, SearchQuery("INV-2026-500", SearchMode.KEYWORD, ANY, 10))
    assert result.hits[0].extraction_id == corpus.extractions["target"]
    assert result.hits[0].score == 1.0
    assert all(h.score < 1.0 for h in result.hits[1:])
