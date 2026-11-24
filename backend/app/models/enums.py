"""Python mirrors of every TEXT+CHECK enum-like column.

Adding a value: add it here AND write a migration that drops/re-creates the
matching ck_<table>_<column> constraint (use NOT VALID + VALIDATE on big tables).
tests/db/test_migrations.py asserts the DB CHECKs match these enums.
"""

from enum import StrEnum


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ErrorCode(StrEnum):
    CORRUPT_FILE = "CORRUPT_FILE"
    PASSWORD_PROTECTED = "PASSWORD_PROTECTED"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    NO_TEXT_FOUND = "NO_TEXT_FOUND"
    OCR_ERROR = "OCR_ERROR"
    LLM_ERROR = "LLM_ERROR"
    EMBEDDING_ERROR = "EMBEDDING_ERROR"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class UnitType(StrEnum):
    PDF_PAGE = "pdf_page"
    IMAGE_FRAME = "image_frame"
    SHEET = "sheet"
    CSV = "csv"


class ExtractionMethod(StrEnum):
    DIGITAL = "digital"
    OCR = "ocr"
    SPREADSHEET = "spreadsheet"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class FieldSource(StrEnum):
    """Value of field_meta[<field>].source (JSONB; not DB-enforced)."""

    LLM = "llm"
    RULE = "rule"
    MANUAL = "manual"


class RuleCode(StrEnum):
    MISSING_FIELD = "MISSING_FIELD"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    LINE_SUM_MISMATCH = "LINE_SUM_MISMATCH"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNGROUNDED_VALUE = "UNGROUNDED_VALUE"
    CROSS_DOC_MISMATCH = "CROSS_DOC_MISMATCH"
    INVALID_DATE = "INVALID_DATE"
    INVALID_CURRENCY = "INVALID_CURRENCY"
    INVALID_FORMAT = "INVALID_FORMAT"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class JobType(StrEnum):
    FULL = "full"
    REPROCESS = "reprocess"
    REEXTRACT = "reextract"
    REVALIDATE = "revalidate"
    REEMBED = "reembed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(StrEnum):
    INGEST = "ingest"
    PARSE = "parse"
    OCR = "ocr"
    SEGMENT = "segment"
    EXTRACT = "extract"
    VALIDATE = "validate"
    EMBED = "embed"
    RECONCILE = "reconcile"


class EventStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class MatchMethod(StrEnum):
    INVOICE_NUMBER = "invoice_number"
    FUZZY = "fuzzy"


class ReconciliationStatus(StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
