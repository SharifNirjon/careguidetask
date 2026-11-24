"""Keyword, semantic, trigram and hybrid search; plus every SQL example in the docs."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import EMBEDDING_DIM, EMBEDDING_MODEL_DEFAULT
from app.db.seed import fake_embedding, seed
from app.models import Chunk, Document, Extraction
from tests.db.factories import make_document, make_unit

DOCS = Path(__file__).resolve().parents[2] / "docs" / "database.md"


def _basis(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _chunk(db: Session, content: str, embedding: list[float] | None = None) -> Chunk:
    doc = make_document(db)
    unit = make_unit(db, doc, text=content)
    c = Chunk(
        document_id=doc.id, unit_id=unit.id, chunk_index=0, content=content,
        embedding=embedding, embedding_model=EMBEDDING_MODEL_DEFAULT if embedding else None,
    )
    db.add(c)
    db.flush()
    return c


def test_keyword_search_matches_invoice_codes(db: Session) -> None:
    hit = _chunk(db, "Invoice INV-2024-001 from ACME Supplies")
    _chunk(db, "Invoice INV-2024-777 from Globex")
    q = text(
        "SELECT id FROM chunks WHERE tsv @@ websearch_to_tsquery('simple', :q)"
    )
    assert db.scalars(q, {"q": "INV-2024-001"}).all() == [hit.id]
    assert db.scalars(q, {"q": "acme"}).all() == [hit.id]  # case-insensitive


def test_simple_config_does_not_stem(db: Session) -> None:
    """Documented trade-off: no stemming, so plural/singular don't match by keyword."""
    _chunk(db, "three invoices attached")
    q = text("SELECT count(*) FROM chunks WHERE tsv @@ websearch_to_tsquery('simple', :q)")
    assert db.scalar(q, {"q": "invoices"}) == 1
    assert db.scalar(q, {"q": "invoice"}) == 0


def test_tsv_is_generated_and_tracks_content(db: Session) -> None:
    c = _chunk(db, "alpha")
    db.execute(text("UPDATE chunks SET content = 'beta' WHERE id = :id"), {"id": c.id})
    tsv = db.scalar(text("SELECT tsv::text FROM chunks WHERE id = :id"), {"id": c.id})
    assert tsv == "'beta':1"


def test_semantic_search_orders_by_cosine_distance(db: Session) -> None:
    near = _chunk(db, "near", [0.9 if i == 0 else (0.1 if i == 1 else 0.0) for i in range(EMBEDDING_DIM)])
    far = _chunk(db, "far", _basis(1))
    _chunk(db, "not embedded yet")
    query = _basis(0)
    ranked = db.scalars(
        select(Chunk.id)
        .where(Chunk.embedding_model == EMBEDDING_MODEL_DEFAULT)
        .order_by(Chunk.embedding.cosine_distance(query))
    ).all()
    assert ranked == [near.id, far.id]


def test_hnsw_index_is_usable_for_knn(db: Session) -> None:
    db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        db.scalars(
            text(
                "EXPLAIN SELECT id FROM chunks "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT 5"
            ),
            {"v": str(_basis(0))},
        ).all()
    )
    assert "ix_chunks_embedding_hnsw" in plan


def test_trigram_fuzzy_supplier_match(db: Session) -> None:
    doc = make_document(db)
    for i, name in enumerate(["acme supplies", "acme suplies gmbh", "globex corporation"]):
        db.add(Extraction(document_id=doc.id, invoice_index=i, unit_start=0, unit_end=0,
                          supplier_name_norm=name))
    db.flush()
    rows = db.execute(
        text(
            "SELECT supplier_name_norm FROM extractions "
            "WHERE supplier_name_norm % :s ORDER BY similarity(supplier_name_norm, :s) DESC"
        ),
        {"s": "acme supplies"},
    ).scalars().all()
    assert rows == ["acme supplies", "acme suplies gmbh"]


def _doc_sql_blocks() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", DOCS.read_text(encoding="utf-8"), flags=re.S)


def test_docs_sql_examples_execute(db: Session) -> None:
    """Every ```sql block in docs/database.md runs and returns rows on seed data."""
    seed(db)
    pdf_id = db.scalar(
        select(Document.id).where(Document.storage_key == "seed/acme_invoices_march.pdf")
    )
    page0 = db.scalar(
        select(Chunk.content).where(Chunk.document_id == pdf_id, Chunk.chunk_index == 0)
    )
    params = {
        "q": "INV-2024-001",
        "query_embedding": str(fake_embedding(page0)),
        "model": EMBEDDING_MODEL_DEFAULT,
        "limit": 10,
        "document_id": pdf_id,
        "supplier_norm": "acme suppliez",
        "invoice_norm": "inv2024001",
    }
    blocks = _doc_sql_blocks()
    assert len(blocks) >= 7
    for sql in blocks:
        names = set(re.findall(r"(?<![:\w]):(\w+)", sql))
        rows = db.execute(text(sql), {k: params[k] for k in names}).mappings().all()
        assert rows, f"no rows for docs query:\n{sql}"

    # Spot-check semantics of the hybrid query: the exact page ranks first in both lists.
    hybrid = next(b for b in blocks if "rrf_score" in b)
    names = set(re.findall(r"(?<![:\w]):(\w+)", hybrid))
    top = db.execute(text(hybrid), {k: params[k] for k in names}).mappings().first()
    assert top is not None
    assert (top["keyword_rank"], top["semantic_rank"]) == (1, 1)
