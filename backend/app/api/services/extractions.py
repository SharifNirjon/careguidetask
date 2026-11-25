"""Read-side use cases for one extraction: detail, versions, correction history."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.api.errors import NotFound
from app.api.repositories.extractions import ExtractionRepository
from app.api.schemas.results import CorrectionOut, ExtractionOut, ExtractionVersionOut
from app.api.services import views
from app.models import Document, Extraction


class ExtractionQueries:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = ExtractionRepository(session)

    def _load(self, extraction_id: uuid.UUID) -> Extraction:
        extraction = self._repo.get(extraction_id)
        if (
            extraction is None
            or self._session.get_one(Document, extraction.document_id).deleted_at is not None
        ):
            raise NotFound(f"Extraction {extraction_id} was not found.", code="extraction_not_found")
        return extraction

    def get(self, extraction_id: uuid.UUID) -> ExtractionOut:
        extraction = self._load(extraction_id)
        ids = [extraction.id]
        return views.extraction_out(
            extraction, self._repo.active_issues(ids), self._repo.reconciliations(ids)
        )

    def versions(self, extraction_id: uuid.UUID) -> list[ExtractionVersionOut]:
        return [views.version_out(v) for v in self._repo.versions(self._load(extraction_id))]

    def corrections(self, extraction_id: uuid.UUID) -> list[CorrectionOut]:
        return [views.correction_out(c) for c in self._repo.corrections(self._load(extraction_id).id)]
