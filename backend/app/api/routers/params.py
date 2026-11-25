"""Query parameters shared by GET /documents and GET /search."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final

from fastapi import Query

from app.api.services.filters import DocumentFilters
from app.models.enums import DocumentStatus, ReviewStatus, RuleCode
from app.pipeline.types import FileKind

MAX_FILTER_CHARS: Final = 200
PAGE_MAX: Final = 100
PAGE_DEFAULT: Final = 25
MONEY_BOUND: Final = Decimal("99999999999999.9999")

Limit = Annotated[int, Query(ge=1, le=PAGE_MAX, description="Page size (max 100).")]
Cursor = Annotated[str | None, Query(max_length=512, description="next_cursor from the previous page.")]
Text = Annotated[str | None, Query(min_length=1, max_length=MAX_FILTER_CHARS)]
Amount = Annotated[Decimal | None, Query(ge=-MONEY_BOUND, le=MONEY_BOUND, examples=["100.00"])]


def document_filters(
    status: DocumentStatus | None = None,
    review_status: ReviewStatus | None = None,
    file_type: FileKind | None = None,
    supplier: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=MAX_FILTER_CHARS,
            description="Fuzzy (trigram) match on the supplier name.",
        ),
    ] = None,
    customer: Text = None,
    invoice_number: Text = None,
    invoice_date_from: date | None = None,
    invoice_date_to: date | None = None,
    total_min: Amount = None,
    total_max: Amount = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
    has_open_issues: bool | None = None,
    rule_code: Annotated[RuleCode | None, Query(description="Has an open issue with this rule.")] = None,
    uploaded_from: datetime | None = None,
    uploaded_to: datetime | None = None,
) -> DocumentFilters:
    return DocumentFilters(
        status=status,
        review_status=review_status,
        file_type=file_type,
        supplier=supplier,
        customer=customer,
        invoice_number=invoice_number,
        invoice_date_from=invoice_date_from,
        invoice_date_to=invoice_date_to,
        total_min=total_min,
        total_max=total_max,
        currency=currency,
        has_open_issues=has_open_issues,
        rule_code=rule_code,
        uploaded_from=uploaded_from,
        uploaded_to=uploaded_to,
    )
