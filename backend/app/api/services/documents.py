"""Document use cases: upload, list, detail/progress, events, file, preview, results, delete."""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from app.api.auth import Principal
from app.api.container import Container
from app.api.errors import BadRequest, FieldError, NotFound, PayloadTooLarge, ValidationFailed
from app.api.pagination import decode_cursor, encode_cursor, invalid_cursor
from app.api.repositories.documents import DocumentRepository, IssueCounts
from app.api.repositories.extractions import ExtractionRepository
from app.api.schemas.common import Page
from app.api.schemas.documents import (
    DocumentDetail,
    DocumentError,
    DocumentSummary,
    EventOut,
    TableBlockOut,
    TableRowOut,
    UnitTableOut,
    UploadError,
    UploadOutcome,
)
from app.api.schemas.documents import IssueCounts as IssueCountsOut
from app.api.schemas.results import DocumentResults
from app.api.services import views
from app.api.services.filters import SortField, SortOrder
from app.api.services.idempotency import IdempotencyStore
from app.api.services.progress import EventView, JobView, compute_progress
from app.models import Document, Extraction
from app.models.enums import UnitType
from app.pipeline.errors import PipelineError
from app.pipeline.intake import ingest_file
from app.pipeline.preview import render_png
from app.pipeline.storage import derived_key
from app.pipeline.types import FileKind
from app.search.filters import DocumentFilters
from app.workers.dispatch import FromStage

log = logging.getLogger(__name__)

LIST_CURSOR_ARITY = 3  # (sort value, created_at, id)
EVENT_CURSOR_ARITY = 2  # (created_at, id)
VISUAL_UNITS = frozenset({UnitType.PDF_PAGE, UnitType.IMAGE_FRAME})
TABULAR_UNITS = frozenset({UnitType.SHEET, UnitType.CSV})
MAX_TABLE_ROWS = 2000  # a review screen, not an export: bounded response size


@dataclass(frozen=True, slots=True)
class IncomingFile:
    """An upload part, already spooled by the web layer (never fully in memory)."""

    filename: str
    stream: BinaryIO
    size: int | None


@dataclass(frozen=True, slots=True)
class FileDownload:
    stream: BinaryIO
    media_type: str
    filename: str
    size: int


def _not_found(document_id: uuid.UUID) -> NotFound:
    return NotFound(f"Document {document_id} was not found.", code="document_not_found")


def _parse_sort_value(sort: SortField, raw: Any) -> Any:
    if sort is SortField.INVOICE_DATE:
        return date.fromisoformat(str(raw))
    if sort is SortField.TOTAL:
        return Decimal(str(raw))
    return datetime.fromisoformat(str(raw))


class DocumentService:
    def __init__(self, session: Session, container: Container) -> None:
        self._session = session
        self._c = container
        self._docs = DocumentRepository(session)

    # ------------------------------------------------------------- upload

    def upload(
        self, files: Sequence[IncomingFile], principal: Principal, idempotency_key: str | None
    ) -> list[UploadOutcome]:
        if not files:
            raise ValidationFailed(
                "Attach at least one file.",
                code="no_files",
                errors=[FieldError(("body", "files"), "at least one file is required")],
            )
        if len(files) > self._c.api.max_files_per_upload:
            raise BadRequest(
                f"At most {self._c.api.max_files_per_upload} files per upload.", code="too_many_files"
            )
        limit = self._c.pipeline.max_file_size_bytes
        too_big = [f.filename for f in files if f.size is not None and f.size > limit]
        if too_big:  # decided before anything is ingested: no partial batch to clean up
            raise PayloadTooLarge(
                f"{len(too_big)} file(s) exceed the {limit // (1024 * 1024)} MB limit.",
                code="file_too_large",
                errors=[FieldError(("body", "files", name), "file too large") for name in too_big],
            )

        store = IdempotencyStore(self._c.redis, ttl_s=self._c.api.idempotency_ttl_s)
        if idempotency_key is not None:
            stored = store.begin(principal.key_id, idempotency_key)
            if stored is not None:
                return [UploadOutcome.model_validate(item) for item in stored]
        try:
            outcomes = [self._ingest_one(f, principal) for f in files]
        except BaseException:
            if idempotency_key is not None:
                store.abort(principal.key_id, idempotency_key)
            raise
        if idempotency_key is not None:
            store.complete(principal.key_id, idempotency_key, [o.model_dump(mode="json") for o in outcomes])
        return outcomes

    def _ingest_one(self, incoming: IncomingFile, principal: Principal) -> UploadOutcome:
        session = self._c.session_factory()
        try:
            document = ingest_file(
                incoming.stream,
                incoming.filename,
                principal.name,
                session,
                storage=self._c.storage,
                enqueue=self._enqueue_parse,
                settings=self._c.pipeline,
            )
            return UploadOutcome(
                filename=document.original_filename,
                document_id=document.id,
                status="queued",
                duplicate_of_document_id=document.duplicate_of_document_id,
            )
        except PipelineError as exc:  # rejected at intake: nothing was stored
            return UploadOutcome(
                filename=incoming.filename,
                status="rejected",
                error=UploadError(code=exc.code, detail=exc.message),
            )
        finally:
            session.close()

    def _enqueue_parse(self, document_id: uuid.UUID, job_id: uuid.UUID) -> None:
        """The rows are committed; a broker hiccup leaves the job queued for the sweeper."""
        try:
            self._c.enqueuer.enqueue(document_id, job_id, FromStage.PARSE)
        except Exception:
            log.warning(
                "enqueue failed; the sweeper will retry",
                extra={"document_id": str(document_id), "job_id": str(job_id)},
                exc_info=True,
            )

    # --------------------------------------------------------------- reads

    def list_page(
        self, filters: DocumentFilters, *, sort: SortField, order: SortOrder, limit: int, cursor: str | None
    ) -> Page[DocumentSummary]:
        decoded = decode_cursor(cursor, arity=LIST_CURSOR_ARITY)
        after = None
        if decoded is not None:
            raw_sort, raw_created, raw_id = decoded.values
            try:
                after = (
                    _parse_sort_value(sort, raw_sort),
                    datetime.fromisoformat(str(raw_created)),
                    uuid.UUID(str(raw_id)),
                )
            except (ValueError, ArithmeticError) as exc:
                raise invalid_cursor() from exc
        rows = self._docs.list_page(filters, sort=sort, order=order, limit=limit + 1, after=after)
        page, more = rows[:limit], len(rows) > limit
        extractions = self._docs.current_extractions([r.document.id for r in page])
        counts = self._docs.open_issue_counts([e.id for e in extractions])
        by_doc: dict[uuid.UUID, list[Extraction]] = {}
        for extraction in extractions:
            by_doc.setdefault(extraction.document_id, []).append(extraction)
        items = [
            views.document_summary(
                r.document,
                [views.headline(e, _counts(counts.get(e.id))) for e in by_doc.get(r.document.id, [])],
            )
            for r in page
        ]
        next_cursor = None
        if more and page:
            last = page[-1]
            next_cursor = encode_cursor([last.sort_value, last.document.created_at, last.document.id])
        return Page[DocumentSummary](items=items, next_cursor=next_cursor)

    def get(self, document_id: uuid.UUID) -> Document:
        document = self._docs.get(document_id)
        if document is None:
            raise _not_found(document_id)
        return document

    def detail(self, document_id: uuid.UUID) -> DocumentDetail:
        document = self.get(document_id)
        job = self._docs.latest_job(document_id)
        events = self._docs.job_events(job.id) if job else []
        progress = compute_progress(
            JobView(job.job_type, job.status, job.started_at, job.finished_at) if job else None,
            [EventView(e.stage, e.status, e.details or {}) for e in events],
            units_total=document.page_count,
            now=datetime.now(UTC),
        )
        error = (
            DocumentError(code=document.error_code, message=document.error_message or "")
            if document.error_code
            else None
        )
        return DocumentDetail(
            id=document.id,
            filename=document.original_filename,
            file_type=views.file_type(document),
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
            error=error,
            page_count=document.page_count,
            unit_count=self._docs.unit_count(document_id),
            uploaded_by=document.uploaded_by,
            duplicate_of_document_id=document.duplicate_of_document_id,
            created_at=document.created_at,
            updated_at=document.updated_at,
            processing_started_at=document.processing_started_at,
            processing_finished_at=document.processing_finished_at,
            latest_job=views.job_out(job) if job else None,
            progress=progress,
        )

    def events(self, document_id: uuid.UUID, *, limit: int, cursor: str | None) -> Page[EventOut]:
        self.get(document_id)
        decoded = decode_cursor(cursor, arity=EVENT_CURSOR_ARITY)
        after = None
        if decoded is not None:
            try:
                after = (datetime.fromisoformat(str(decoded.values[0])), int(decoded.values[1]))
            except ValueError as exc:
                raise invalid_cursor() from exc
        rows = self._docs.events(document_id, limit=limit + 1, after=after)
        page = rows[:limit]
        next_cursor = (
            encode_cursor([page[-1].created_at, page[-1].id]) if len(rows) > limit and page else None
        )
        return Page[EventOut](items=[views.event_out(e) for e in page], next_cursor=next_cursor)

    def results(self, document_id: uuid.UUID) -> DocumentResults:
        document = self.get(document_id)
        repo = ExtractionRepository(self._session)
        extractions = repo.current_for_document(document_id)
        ids = [e.id for e in extractions]
        issues, recs = repo.active_issues(ids), repo.reconciliations(ids)
        return DocumentResults(
            document_id=document.id,
            status=document.status,
            extractions=[views.extraction_out(e, issues, recs) for e in extractions],
        )

    # --------------------------------------------------------------- files

    def file(self, document_id: uuid.UUID) -> FileDownload:
        document = self.get(document_id)
        return FileDownload(
            stream=self._c.storage.open(document.storage_key),
            media_type=document.mime_type,
            filename=document.original_filename,
            size=document.size_bytes,
        )

    def unit_table(self, document_id: uuid.UUID, unit_index: int) -> UnitTableOut:
        """Parsed rows of a sheet/CSV unit, grouped by table block (from structured_data)."""
        self.get(document_id)
        unit = self._docs.unit(document_id, unit_index)
        if unit is None or unit.unit_type not in TABULAR_UNITS:
            raise NotFound(f"Document {document_id} has no sheet {unit_index}.", code="unit_not_found")
        data = unit.structured_data or {}
        rows: list[dict[str, Any]] = data.get("rows") or []
        blocks = []
        shown = 0
        for block in data.get("table_blocks") or []:
            headers: list[str] = list(block.get("headers") or [])
            block_rows = [r for r in rows if r.get("_block") == block.get("index")][: MAX_TABLE_ROWS - shown]
            shown += len(block_rows)
            blocks.append(
                TableBlockOut(
                    headers=headers,
                    rows=[
                        TableRowOut(
                            row_number=int(r.get("_row", 0)), cells=[str(r.get(h, "")) for h in headers]
                        )
                        for r in block_rows
                    ],
                )
            )
        return UnitTableOut(
            unit_index=unit.unit_index, sheet_name=unit.sheet_name, blocks=blocks, truncated=shown < len(rows)
        )

    def unit_image(self, document_id: uuid.UUID, unit_index: int, dpi: int) -> BinaryIO:
        api = self._c.api
        if not api.preview_dpi_min <= dpi <= api.preview_dpi_max:
            raise ValidationFailed(
                f"dpi must be between {api.preview_dpi_min} and {api.preview_dpi_max}.",
                code="dpi_out_of_range",
                errors=[FieldError(("query", "dpi"), "out of range")],
            )
        document = self.get(document_id)
        unit = self._docs.unit(document_id, unit_index)
        if unit is None or unit.unit_type not in VISUAL_UNITS:
            raise NotFound(
                f"Document {document_id} has no page or frame {unit_index}.", code="unit_not_found"
            )
        storage = self._c.storage
        key = derived_key("preview", str(document.id), unit.unit_type.value, str(unit_index), str(dpi))
        if not storage.exists(key):
            with storage.local_path(document.storage_key) as path:
                try:
                    png = render_png(
                        FileKind.from_mime(document.mime_type), path, unit_index, dpi, self._c.pipeline
                    )
                except IndexError as exc:
                    raise NotFound(
                        f"Document {document_id} has no page or frame {unit_index}.", code="unit_not_found"
                    ) from exc
            storage.put(io.BytesIO(png), key=key)
        return storage.open(key)

    # --------------------------------------------------------------- admin

    def delete(self, document_id: uuid.UUID, principal: Principal) -> None:
        document = self.get(document_id)
        self._docs.soft_delete(document, datetime.now(UTC))
        self._session.commit()
        log.info("document deleted", extra={"document_id": str(document_id), "principal": principal.name})


def _counts(counts: IssueCounts | None) -> IssueCountsOut:
    counts = counts or IssueCounts()
    return IssueCountsOut(error=counts.error, warning=counts.warning, info=counts.info)
