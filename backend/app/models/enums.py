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
    # permanent: the file itself is the problem
    CORRUPT_FILE = "CORRUPT_FILE"
    PASSWORD_PROTECTED = "PASSWORD_PROTECTED"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    EMPTY_FILE = "EMPTY_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    DECOMPRESSION_BOMB = "DECOMPRESSION_BOMB"
    NO_TEXT_FOUND = "NO_TEXT_FOUND"
    # stage / infrastructure failures
    STORAGE_ERROR = "STORAGE_ERROR"
    OCR_ERROR = "OCR_ERROR"
    LLM_ERROR = "LLM_ERROR"
    LLM_QUOTA_DEFERRED = "LLM_QUOTA_DEFERRED"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    NOT_AN_INVOICE = "NOT_AN_INVOICE"
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
    # completeness & trust
    MISSING_FIELD = "MISSING_FIELD"
    UNGROUNDED_VALUE = "UNGROUNDED_VALUE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NOT_AN_INVOICE = "NOT_AN_INVOICE"
    # numeric validity
    INVALID_AMOUNT = "INVALID_AMOUNT"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    SIGN_INCONSISTENT = "SIGN_INCONSISTENT"
    IMPLAUSIBLE_TAX_RATE = "IMPLAUSIBLE_TAX_RATE"
    ZERO_TOTAL = "ZERO_TOTAL"
    # arithmetic consistency
    LINE_AMOUNT_MISMATCH = "LINE_AMOUNT_MISMATCH"
    LINE_SUM_MISMATCH = "LINE_SUM_MISMATCH"
    TAX_INCLUSIVE_LINES = "TAX_INCLUSIVE_LINES"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    # dates & formats
    INVALID_DATE = "INVALID_DATE"
    FUTURE_DATE = "FUTURE_DATE"
    DATE_TOO_OLD = "DATE_TOO_OLD"
    UNKNOWN_CURRENCY = "UNKNOWN_CURRENCY"
    INVOICE_NUMBER_FORMAT = "INVOICE_NUMBER_FORMAT"
    # duplicates & cross-document
    DUPLICATE_FILE = "DUPLICATE_FILE"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    INVOICE_NUMBER_CONFLICT = "INVOICE_NUMBER_CONFLICT"
    CROSS_DOC_VERIFIED = "CROSS_DOC_VERIFIED"
    CROSS_DOC_MISMATCH = "CROSS_DOC_MISMATCH"
    # pre-Step-5 codes: still accepted so existing rows stay valid; no rule emits them
    INVALID_CURRENCY = "INVALID_CURRENCY"
    INVALID_FORMAT = "INVALID_FORMAT"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ChunkKind(StrEnum):
    CONTENT = "content"  # text of one document unit (page / frame / sheet rows)
    EXTRACTION_SUMMARY = "extraction_summary"  # normalized fields of one current extraction


class JobType(StrEnum):
    FULL = "full"
    REPROCESS = "reprocess"
    REEXTRACT = "reextract"
    REVALIDATE = "revalidate"
    REEMBED = "reembed"


class ApiRole(StrEnum):
    """Ordered: each role includes everything the previous one may do."""

    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return list(ApiRole).index(self)

    def allows(self, required: "ApiRole") -> bool:
        return self.rank >= required.rank


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


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
