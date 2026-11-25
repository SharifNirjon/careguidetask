"""/documents: thin HTTP mapping over DocumentService and JobService."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated, BinaryIO, Final
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Header, Path, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import AdminWrite, ContainerDep, ReviewerUpload, ReviewerWrite, SessionDep, Viewer
from app.api.etag import conditional_json
from app.api.routers.params import PAGE_DEFAULT, Cursor, Limit, document_filters
from app.api.schemas.common import Page, problems
from app.api.schemas.documents import DocumentDetail, DocumentSummary, EventOut, UploadOutcome
from app.api.schemas.jobs import JobOut, ReprocessRequest
from app.api.schemas.results import DocumentResults
from app.api.services.documents import DocumentService, IncomingFile
from app.api.services.filters import SortField, SortOrder
from app.api.services.jobs import JobService
from app.search.filters import DocumentFilters

router = APIRouter(prefix="/documents", tags=["documents"])

STREAM_CHUNK: Final = 64 * 1024
_UNSAFE_FILENAME_CHARS: Final = str.maketrans({c: "_" for c in '"\\\r\n;'})
DocumentId = Annotated[uuid.UUID, Path(description="Document id.")]


def _stream(handle: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := handle.read(STREAM_CHUNK):
            yield chunk
    finally:
        handle.close()


def content_disposition(filename: str) -> str:
    """inline; ASCII fallback + RFC 5987 UTF-8 name. No quotes/CR/LF can break out of the header."""
    ascii_name = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .replace("?", "_")
        .translate(_UNSAFE_FILENAME_CHARS)
        or "download"
    )
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=list[UploadOutcome],
    summary="Upload documents",
    description=(
        "Multipart upload of 1..MAX_FILES_PER_UPLOAD files. Returns one outcome per file; one bad file "
        "never fails the batch. Send `Idempotency-Key` to make retries safe: a replay returns the "
        "original response and ingests nothing."
    ),
    responses=problems(400, 403, 409, 413),
)
def upload_documents(
    files: Annotated[list[UploadFile], File(description="The files to ingest.")],
    principal: ReviewerUpload,
    session: SessionDep,
    container: ContainerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> list[UploadOutcome]:
    incoming = [IncomingFile(filename=f.filename or "unnamed", stream=f.file, size=f.size) for f in files]
    return DocumentService(session, container).upload(incoming, principal, idempotency_key)


@router.get(
    "",
    response_model=Page[DocumentSummary],
    summary="List documents",
    description="Filtered, sorted, cursor-paginated summaries (headline invoice fields and open issue "
    "counts; no line items, no text).",
    responses=problems(403),
)
def list_documents(
    _: Viewer,
    session: SessionDep,
    container: ContainerDep,
    filters: Annotated[DocumentFilters, Depends(document_filters)],
    sort: SortField = SortField.CREATED_AT,
    order: SortOrder = SortOrder.DESC,
    limit: Limit = PAGE_DEFAULT,
    cursor: Cursor = None,
) -> Page[DocumentSummary]:
    return DocumentService(session, container).list_page(
        filters, sort=sort, order=order, limit=limit, cursor=cursor
    )


@router.get(
    "/{document_id}",
    response_model=DocumentDetail,
    summary="Get a document",
    description="Metadata, status, error and live progress. Supports ETag / If-None-Match (304).",
    responses={304: {"description": "Not modified"}, **problems(403, 404)},
)
def get_document(
    document_id: DocumentId, request: Request, _: Viewer, session: SessionDep, container: ContainerDep
) -> Response:
    detail = DocumentService(session, container).detail(document_id)
    return conditional_json(request, detail, etag_basis=detail.etag_basis())


@router.get(
    "/{document_id}/events",
    response_model=Page[EventOut],
    summary="Processing history",
    responses=problems(403, 404),
)
def list_events(
    document_id: DocumentId,
    _: Viewer,
    session: SessionDep,
    container: ContainerDep,
    limit: Limit = PAGE_DEFAULT,
    cursor: Cursor = None,
) -> Page[EventOut]:
    return DocumentService(session, container).events(document_id, limit=limit, cursor=cursor)


@router.get(
    "/{document_id}/file",
    summary="Download the original file",
    response_class=StreamingResponse,
    responses={
        200: {"description": "The original bytes", "content": {"application/octet-stream": {}}},
        **problems(403, 404),
    },
)
def download_file(
    document_id: DocumentId, _: Viewer, session: SessionDep, container: ContainerDep
) -> StreamingResponse:
    download = DocumentService(session, container).file(document_id)
    return StreamingResponse(
        _stream(download.stream),
        media_type=download.media_type,
        headers={
            "Content-Disposition": content_disposition(download.filename),
            "Content-Length": str(download.size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0",
        },
    )


@router.get(
    "/{document_id}/units/{unit_index}/image",
    summary="Page / frame preview (PNG)",
    response_class=StreamingResponse,
    description="Renders a PDF page or image frame for the UI. Cached in storage. 404 for spreadsheet units.",
    responses={200: {"content": {"image/png": {}}, "description": "PNG"}, **problems(403, 404)},
)
def unit_image(
    document_id: DocumentId,
    unit_index: Annotated[int, Path(ge=0, le=100_000)],
    _: Viewer,
    session: SessionDep,
    container: ContainerDep,
    dpi: Annotated[int, Query(ge=1, le=1200, description="Bounded by PREVIEW_DPI_MIN..MAX.")] = 110,
) -> StreamingResponse:
    image = DocumentService(session, container).unit_image(document_id, unit_index, dpi)
    return StreamingResponse(
        _stream(image), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get(
    "/{document_id}/results",
    response_model=DocumentResults,
    summary="Extraction results",
    description="Current extractions with per-field value/raw/confidence/evidence, line items, active "
    "issues and reconciliations. Supports ETag / If-None-Match (304).",
    responses={304: {"description": "Not modified"}, **problems(403, 404)},
)
def get_results(
    document_id: DocumentId, request: Request, _: Viewer, session: SessionDep, container: ContainerDep
) -> Response:
    return conditional_json(request, DocumentService(session, container).results(document_id))


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document (soft)",
    responses=problems(403, 404),
)
def delete_document(
    document_id: DocumentId, principal: AdminWrite, session: SessionDep, container: ContainerDep
) -> Response:
    DocumentService(session, container).delete(document_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{document_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
    summary="Reprocess a document",
    tags=["jobs"],
    description="Creates a job from the given stage and enqueues it. 409 if a job is already queued "
    "or running (enforced by a unique index).",
    responses=problems(403, 404, 409),
)
def reprocess_document(
    document_id: DocumentId,
    body: ReprocessRequest,
    principal: ReviewerWrite,
    session: SessionDep,
    container: ContainerDep,
) -> JobOut:
    return JobService(session, container).reprocess(document_id, body.from_stage, body.force, principal)
