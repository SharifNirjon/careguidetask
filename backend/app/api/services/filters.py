"""Document list/search filters and sorting (plain data, shared by list and search)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.models.enums import DocumentStatus, ReviewStatus, RuleCode
from app.pipeline.types import FileKind


class SortField(StrEnum):
    CREATED_AT = "created_at"
    INVOICE_DATE = "invoice_date"  # of the document's first invoice
    TOTAL = "total"  # of the document's first invoice


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class DocumentFilters:
    status: DocumentStatus | None = None
    review_status: ReviewStatus | None = None
    file_type: FileKind | None = None
    supplier: str | None = None
    customer: str | None = None
    invoice_number: str | None = None
    invoice_date_from: date | None = None
    invoice_date_to: date | None = None
    total_min: Decimal | None = None
    total_max: Decimal | None = None
    currency: str | None = None
    has_open_issues: bool | None = None
    rule_code: RuleCode | None = None
    uploaded_from: datetime | None = None
    uploaded_to: datetime | None = None

    @property
    def touches_extractions(self) -> bool:
        return any(
            v is not None
            for v in (
                self.review_status,
                self.supplier,
                self.customer,
                self.invoice_number,
                self.invoice_date_from,
                self.invoice_date_to,
                self.total_min,
                self.total_max,
                self.currency,
            )
        )
