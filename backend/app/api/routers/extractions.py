"""/extractions and /issues: results, corrections and review."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Path, Request, Response

from app.api.deps import ContainerDep, ReviewerWrite, SessionDep, Viewer
from app.api.etag import conditional_json
from app.api.schemas.common import problems
from app.api.schemas.results import (
    ApproveRequest,
    CorrectionOut,
    CorrectionRequest,
    ExtractionOut,
    ExtractionVersionOut,
    IssueOut,
    RejectRequest,
    ResolveRequest,
)
from app.api.services.corrections import CorrectionService
from app.api.services.extractions import ExtractionQueries
from app.api.services.review import ReviewService

router = APIRouter(tags=["extractions"])
ExtractionId = Annotated[uuid.UUID, Path(description="Extraction id.")]
IssueId = Annotated[uuid.UUID, Path(description="Issue id.")]


@router.get(
    "/extractions/{extraction_id}",
    response_model=ExtractionOut,
    summary="Get an extraction",
    responses={304: {"description": "Not modified"}, **problems(403, 404)},
)
def get_extraction(extraction_id: ExtractionId, request: Request, _: Viewer, session: SessionDep) -> Response:
    return conditional_json(request, ExtractionQueries(session).get(extraction_id))


@router.get(
    "/extractions/{extraction_id}/versions",
    response_model=list[ExtractionVersionOut],
    summary="All versions of this invoice",
    responses=problems(403, 404),
)
def list_versions(extraction_id: ExtractionId, _: Viewer, session: SessionDep) -> list[ExtractionVersionOut]:
    return ExtractionQueries(session).versions(extraction_id)


@router.get(
    "/extractions/{extraction_id}/corrections",
    response_model=list[CorrectionOut],
    summary="Correction audit trail",
    responses=problems(403, 404),
)
def list_corrections(extraction_id: ExtractionId, _: Viewer, session: SessionDep) -> list[CorrectionOut]:
    return ExtractionQueries(session).corrections(extraction_id)


@router.patch(
    "/extractions/{extraction_id}",
    response_model=ExtractionOut,
    summary="Correct fields",
    description="Requires `If-Match: <lock_version>` (412 on mismatch, 428 if missing). Only whitelisted "
    "fields; values are parsed with the pipeline's normalizers (422 with field errors). Writes the "
    "audit trail and re-validates in the same transaction. Returns the new result and ETag.",
    responses=problems(403, 404, 409, 412, 428),
)
def correct_extraction(
    extraction_id: ExtractionId,
    body: CorrectionRequest,
    request: Request,
    principal: ReviewerWrite,
    session: SessionDep,
    container: ContainerDep,
    if_match: Annotated[str | None, Header(alias="If-Match", max_length=32)] = None,
) -> Response:
    result = CorrectionService(session, container).correct(extraction_id, body, if_match, principal)
    return conditional_json(request, result)


@router.post(
    "/extractions/{extraction_id}/approve",
    response_model=ExtractionOut,
    summary="Approve",
    description="409 while unresolved errors remain.",
    responses=problems(403, 404, 409),
)
def approve(
    extraction_id: ExtractionId,
    body: ApproveRequest,
    principal: ReviewerWrite,
    session: SessionDep,
    container: ContainerDep,
) -> ExtractionOut:
    return ReviewService(session, container).approve(extraction_id, body.note, principal)


@router.post(
    "/extractions/{extraction_id}/reject",
    response_model=ExtractionOut,
    summary="Reject",
    description="A note is required.",
    responses=problems(403, 404, 409),
)
def reject(
    extraction_id: ExtractionId,
    body: RejectRequest,
    principal: ReviewerWrite,
    session: SessionDep,
    container: ContainerDep,
) -> ExtractionOut:
    return ReviewService(session, container).reject(extraction_id, body.note, principal)


@router.post(
    "/issues/{issue_id}/resolve",
    response_model=IssueOut,
    summary="Resolve an issue",
    tags=["issues"],
    description="Acknowledge an issue with a note; it no longer blocks approval.",
    responses=problems(403, 404, 409),
)
def resolve_issue(
    issue_id: IssueId,
    body: ResolveRequest,
    principal: ReviewerWrite,
    session: SessionDep,
    container: ContainerDep,
) -> IssueOut:
    return ReviewService(session, container).resolve(issue_id, body.note, principal)


@router.post(
    "/issues/{issue_id}/reopen",
    response_model=IssueOut,
    summary="Reopen an issue",
    tags=["issues"],
    responses=problems(403, 404, 409),
)
def reopen_issue(
    issue_id: IssueId, _: ReviewerWrite, session: SessionDep, container: ContainerDep
) -> IssueOut:
    return ReviewService(session, container).reopen(issue_id)
