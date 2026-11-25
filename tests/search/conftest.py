"""Search fixtures: real PostgreSQL (committed data, wiped after each test),
fakeredis, and the deterministic FakeEmbeddingClient."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import fakeredis
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import SearchSettings
from app.models import Document, DocumentUnit, Extraction
from app.search.embedding import ChunkEmbedder, EmbedBudget
from app.search.indexer import ChunkIndexer
from app.search.semantic import QueryEmbedder
from app.search.service import HybridSearchService
from tests.db.factories import make_document, make_unit
from tests.search.fakes import FakeEmbeddingClient, StaticQuota


@pytest.fixture
def session_factory(committed_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=committed_engine, expire_on_commit=False)


@pytest.fixture
def redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def client() -> FakeEmbeddingClient:
    return FakeEmbeddingClient()


@pytest.fixture
def search_settings() -> SearchSettings:
    return SearchSettings(chunk_tokens=120, chunk_overlap_tokens=20)


@pytest.fixture
def make_budget(redis: fakeredis.FakeRedis) -> Callable[..., EmbedBudget]:
    def build(limit: int = 1000, share: float = 1.0) -> EmbedBudget:
        return EmbedBudget(redis, StaticQuota(limit), share=share)

    return build


@pytest.fixture
def make_embedder(
    session_factory: sessionmaker[Session],
    client: FakeEmbeddingClient,
    make_budget: Callable[..., EmbedBudget],
) -> Callable[..., ChunkEmbedder]:
    def build(batch_size: int = 4, **budget: Any) -> ChunkEmbedder:
        return ChunkEmbedder(
            session_factory=session_factory,
            client=client,
            budget=make_budget(**budget),
            batch_size=batch_size,
        )

    return build


@pytest.fixture
def make_service(
    redis: fakeredis.FakeRedis,
    client: FakeEmbeddingClient,
    search_settings: SearchSettings,
    make_budget: Callable[..., EmbedBudget],
) -> Callable[..., HybridSearchService]:
    def build(*, embedder: bool = True, **budget: Any) -> HybridSearchService:
        query_embedder = (
            QueryEmbedder(client=client, redis=redis, budget=make_budget(**budget), ttl_s=60)
            if embedder
            else None
        )
        return HybridSearchService(
            settings=search_settings, min_similarity=0.3, query_embedder=query_embedder
        )

    return build


@dataclass
class Corpus:
    """Committed documents with units + current extractions, indexed for search."""

    session_factory: sessionmaker[Session]
    settings: SearchSettings
    documents: dict[str, uuid.UUID] = field(default_factory=dict)
    extractions: dict[str, uuid.UUID] = field(default_factory=dict)

    def add(
        self,
        name: str,
        pages: list[str],
        *,
        number: str | None = None,
        supplier: str | None = None,
        customer: str | None = None,
        total: str | None = None,
        currency: str | None = "EUR",
        issued: date | None = None,
        mime: str = "application/pdf",
        index: bool = True,
    ) -> uuid.UUID:
        from app.extraction.normalize import invoice_number_norm, supplier_name_norm

        with self.session_factory() as session:
            doc = make_document(session, original_filename=f"{name}.pdf", mime_type=mime)
            for i, text in enumerate(pages):
                make_unit(session, doc, index=i, text=text)
            extraction = Extraction(
                document_id=doc.id,
                invoice_index=0,
                unit_start=0,
                unit_end=max(len(pages) - 1, 0),
                invoice_number=number,
                invoice_number_norm=invoice_number_norm(number),
                supplier_name=supplier,
                supplier_name_norm=supplier_name_norm(supplier),
                customer_name=customer,
                total=Decimal(total) if total else None,
                currency=currency,
                invoice_date=issued,
                field_meta={"_document_type": "invoice"},
            )
            session.add(extraction)
            session.commit()
            self.documents[name], self.extractions[name] = doc.id, extraction.id
        if index:
            self.reindex(name)
        return doc.id

    def reindex(self, name: str) -> None:
        with self.session_factory() as session:
            ChunkIndexer(session, self.settings).rebuild_document(self.documents[name])
            session.commit()

    def unit(self, name: str, index: int) -> DocumentUnit:
        with self.session_factory() as session:
            return (
                session.query(DocumentUnit)
                .filter_by(document_id=self.documents[name], unit_index=index)
                .one()
            )

    def document(self, name: str) -> Document:
        with self.session_factory() as session:
            return session.get_one(Document, self.documents[name])


@pytest.fixture
def corpus(session_factory: sessionmaker[Session], search_settings: SearchSettings) -> Corpus:
    return Corpus(session_factory, search_settings)
