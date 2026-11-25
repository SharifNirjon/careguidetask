"""Sorting for GET /documents (filters live in app.search.filters)."""

from __future__ import annotations

from enum import StrEnum


class SortField(StrEnum):
    CREATED_AT = "created_at"
    INVOICE_DATE = "invoice_date"  # of the document's first invoice
    TOTAL = "total"  # of the document's first invoice


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"
