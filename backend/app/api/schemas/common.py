"""Shared schema types and conventions.

Money is a decimal STRING ("1234.56"), never a float. Timestamps are UTC ISO
8601 with a trailing "Z". Enums are strings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema

T = TypeVar("T")


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f"), return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d+)?$", "examples": ["1234.5600"]}),
]
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(_utc, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time", "examples": ["2026-03-01T12:00:00Z"]}),
]


class ApiModel(BaseModel):
    """Response models: built from dataclasses/dicts, never ORM objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Page(ApiModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(
        None, description="Pass as ?cursor= to get the next page; null = last page."
    )


class FieldErrorOut(ApiModel):
    loc: list[str | int]
    msg: str
    type: str


class ProblemDetail(ApiModel):
    """RFC 9457 problem details (media type application/problem+json)."""

    type: str = Field(examples=["https://careguide.dev/problems/not_found"])
    title: str = Field(examples=["Not Found"])
    status: int = Field(examples=[404])
    detail: str = Field(examples=["Document 3fa2... was not found."])
    instance: str = Field(examples=["/api/v1/documents/3fa2..."])
    code: str = Field(examples=["not_found"])
    request_id: str | None = Field(None, examples=["req-demo-0001"])
    errors: list[FieldErrorOut] = Field(default_factory=list)


_DESCRIPTIONS = {
    400: "Bad request",
    401: "Missing, invalid or revoked API key",
    403: "The key's role does not allow this",
    404: "Not found",
    409: "Conflicts with the current state",
    412: "If-Match does not match the current version",
    413: "Payload too large",
    422: "Invalid fields (see errors[])",
    428: "If-Match header required",
    429: "Rate limit exceeded (see Retry-After)",
    501: "Not implemented yet",
    503: "A dependency is unavailable",
}


def problems(*statuses: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI `responses=` entries for the given error statuses (plus 401/422/429 always)."""
    wanted = sorted({401, 422, 429, *statuses})
    return {
        code: {
            "model": ProblemDetail,
            "description": _DESCRIPTIONS[code],
            "content": {"application/problem+json": {}},
        }
        for code in wanted
    }
