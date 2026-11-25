"""Application configuration.

Runtime settings come from environment variables. EMBEDDING_DIM is deliberately
NOT read from the environment: it is baked into the `chunks.embedding` column
type, so changing it requires a schema migration (see docs/database.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stored embedding dimension. Models with a larger native dimension are
# requested at (or truncated to) this size and L2-normalized; 1024 keeps the
# pgvector HNSW index well under its 2000-dim limit.
# Changing this value requires: a new Alembic migration that alters
# chunks.embedding to VECTOR(<new dim>) (dropping/recreating the HNSW index),
# and a full re-embed of every chunk. tests/db/test_migrations.py fails if the
# database and this constant disagree.
EMBEDDING_DIM: Final[int] = 1024
EMBEDDING_MODEL_DEFAULT: Final[str] = "nvidia/nemotron-3.5-embed-8b:free"
EMBEDDING_SPACE_DEFAULT: Final[str] = "nemotron-8b"


def normalize_db_url(url: str) -> str:
    """Force the psycopg 3 driver; accept plain postgres:// / postgresql:// URLs."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@dataclass(frozen=True, slots=True)
class DBSettings:
    url: str
    pool_size: int
    max_overflow: int
    pool_recycle_s: int
    statement_timeout_ms: int

    @classmethod
    def from_env(cls) -> DBSettings:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set (see .env.example)")
        return cls(
            url=normalize_db_url(url),
            pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
            max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
            pool_recycle_s=int(os.environ.get("DB_POOL_RECYCLE_S", "1800")),
            statement_timeout_ms=int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000")),
        )


class PipelineSettings(BaseSettings):
    """Ingestion/parsing limits and thresholds. Every field is overridable via env.

    Operational limits come from the spec; heuristic thresholds are documented
    (with how they were chosen) in docs/pipeline.md.
    """

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # --- infrastructure ---
    storage_root: Path = Path("/data/storage")
    redis_url: str = "redis://localhost:6379/0"

    # --- intake limits ---
    max_file_size_mb: int = Field(100, gt=0)
    max_zip_uncompressed_mb: int = Field(1024, gt=0)
    max_zip_ratio: float = Field(200.0, gt=1)
    zip_ratio_min_member_mb: int = Field(1, ge=0)  # tiny members may compress extremely well
    intake_chunk_bytes: int = Field(1024 * 1024, gt=0)

    # --- parsing limits ---
    max_pages: int = Field(2000, gt=0)
    page_batch_size: int = Field(25, gt=0)
    max_image_pixels: int = Field(180_000_000, gt=0)
    max_sheet_rows: int = Field(200_000, gt=0)
    max_unit_text_chars: int = Field(200_000, gt=0)
    max_failed_unit_ratio: float = Field(0.2, ge=0, le=1)
    store_layout: bool = True

    # --- PDF page classification ---
    min_digital_chars: int = Field(50, ge=0)
    sparse_text_chars: int = Field(200, ge=0)
    image_coverage_ocr_ratio: float = Field(0.5, ge=0, le=1)
    garbage_min_alnum_ratio: float = Field(0.5, ge=0, le=1)
    garbage_max_replacement_ratio: float = Field(0.02, ge=0, le=1)

    # --- OCR ---
    ocr_dpi: int = Field(300, gt=0)
    ocr_langs: str = "eng"
    ocr_page_timeout_s: int = Field(60, gt=0)
    ocr_min_confidence: float = Field(0.6, ge=0, le=1)
    # OSD costs ~1 s/page. "on_low_confidence": only when attempt 1 scores low
    # (sideways/upside-down text always does); "always": before attempt 1.
    ocr_orientation_mode: Literal["on_low_confidence", "always", "never"] = "on_low_confidence"
    ocr_min_osd_confidence: float = Field(2.0, ge=0)

    # --- preprocessing ---
    min_effective_dpi: int = Field(200, gt=0)
    min_text_height_px: int = Field(20, gt=0)
    max_upscale_factor: float = Field(3.0, ge=1)
    deskew_min_angle_deg: float = Field(0.5, ge=0)
    deskew_max_angle_deg: float = Field(10.0, gt=0)
    noise_speck_ratio: float = Field(0.3, ge=0, le=1)  # share of components that are specks
    speck_max_area_px: int = Field(4, gt=0)
    low_contrast_threshold: float = Field(100.0, ge=0)  # paper-ink gap (0-255)
    uneven_illumination_threshold: float = Field(25.0, ge=0)

    # --- Celery ---
    task_soft_time_limit_s: int = Field(900, gt=0)  # one page batch (25 OCR pages) fits easily
    task_hard_time_limit_s: int = Field(960, gt=0)
    task_max_retries: int = Field(3, ge=0)
    task_retry_backoff_max_s: int = Field(300, gt=0)
    worker_max_tasks_per_child: int = Field(100, gt=0)  # recycle processes: caps leaks
    # Quota deferrals schedule a task ~24 h ahead; with acks_late the Redis
    # visibility timeout must exceed that or the task is redelivered early.
    broker_visibility_timeout_s: int = Field(25 * 3600, gt=0)
    llm_defer_jitter_s: int = Field(900, ge=0)  # spread deferred jobs after the reset

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_pipeline_settings() -> PipelineSettings:
    return PipelineSettings()


class EmbeddingModelConfig(BaseModel):
    """One embedding model and the vector space its outputs live in.

    Two models share a space ONLY if their vectors are directly comparable
    (same weights). Different models are different spaces, even at equal dims.
    """

    model: str
    space: str


class LLMSettings(BaseSettings):
    """OpenRouter gateway configuration. The API key is read from the environment only."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    redis_url: str = "redis://localhost:6379/0"

    # --- chat ---
    llm_primary_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    llm_fallback_models: list[str] = Field(default_factory=list)
    llm_reasoning_effort: str = "low"  # mapped to the nearest effort the model supports
    llm_max_tokens: int = Field(16_000, gt=0)  # generous: reasoning shares this budget
    llm_connect_timeout_s: float = Field(10.0, gt=0)
    llm_read_timeout_s: float = Field(240.0, gt=0)  # reasoning models are slow

    # --- embeddings ---
    embedding_models: list[EmbeddingModelConfig] = Field(
        default_factory=lambda: [
            EmbeddingModelConfig(model="nvidia/nemotron-3.5-embed-8b:free", space="nemotron-3.5-8b"),
            EmbeddingModelConfig(model="nvidia/nemotron-3-embed-1b:free", space="nemotron-3-1b"),
            EmbeddingModelConfig(model="nvidia/nemotron-3.5-embed-1b:free", space="nemotron-3.5-1b"),
        ]
    )
    embed_batch_size: int = Field(32, gt=0)
    embed_chars_per_token: float = Field(2.5, gt=0)  # conservative: caps inputs below the model limit
    embed_input_type_query: str = "search_query"
    embed_input_type_document: str = "search_document"

    # --- retries / fallback ---
    llm_max_attempts: int = Field(3, gt=0)  # per model, for 5xx / timeouts / connection errors
    llm_backoff_base_s: float = Field(2.0, gt=0)
    llm_backoff_max_s: float = Field(30.0, gt=0)
    llm_max_retry_after_s: float = Field(90.0, gt=0)  # longer waits -> try the next model instead
    circuit_failure_threshold: int = Field(3, gt=0)
    circuit_open_s: float = Field(300.0, gt=0)

    # --- free-tier budget (shared by every :free model, embeddings included) ---
    free_model_rpm: int = Field(18, gt=0)  # headroom under OpenRouter's 20/min
    free_model_daily_limit: int = Field(50, gt=0)  # 50 or 1000 depending on credits; resynced
    quota_resync_s: float = Field(300.0, gt=0)
    rate_window_s: float = Field(60.0, gt=0)

    # --- cache / catalog ---
    llm_cache_ttl_s: int = Field(7 * 24 * 3600, gt=0)
    model_catalog_ttl_s: int = Field(24 * 3600, gt=0)

    @property
    def chat_models(self) -> list[str]:
        return [self.llm_primary_model, *self.llm_fallback_models]


class ExtractionSettings(BaseSettings):
    """Extraction behaviour: prompt budget, grounding and confidence fusion."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    max_prompt_chars: int = Field(60_000, gt=0)  # ~15-20k tokens of document text
    grounding_min_score: float = Field(0.85, ge=0, le=1)  # tolerates light OCR noise
    ambiguous_parse_confidence_factor: float = Field(0.5, ge=0, le=1)  # e.g. "1,234" or 03/04/2024
    field_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "invoice_number": 0.25,
            "invoice_date": 0.15,
            "supplier_name": 0.20,
            "total": 0.30,
            "currency": 0.10,
        }
    )


@lru_cache(maxsize=1)
def get_llm_settings() -> LLMSettings:
    return LLMSettings()


@lru_cache(maxsize=1)
def get_extraction_settings() -> ExtractionSettings:
    return ExtractionSettings()


class ValidationSettings(BaseSettings):
    """Rule thresholds, required fields, review policy. All overridable via env (JSON for dicts/lists)."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # --- money ---
    validation_rel_tol: float = Field(0.005, ge=0, le=1)  # 0.5% of the larger amount

    # --- trust ---
    field_confidence_min: float = Field(0.6, ge=0, le=1)
    overall_confidence_min: float = Field(0.7, ge=0, le=1)

    # --- plausibility ---
    max_tax_rate: float = Field(0.5, ge=0)
    future_date_grace_days: int = Field(7, ge=0)  # issue date vs "today" across time zones/back-office lag
    max_invoice_age_years: int = Field(5, gt=0)
    invoice_number_min_len: int = Field(2, gt=0)
    invoice_number_max_len: int = Field(40, gt=0)

    # --- duplicates / cross-document ---
    supplier_match_min: float = Field(0.8, ge=0, le=1)  # pg_trgm similarity on supplier_name_norm
    duplicate_date_window_days: int = Field(3, ge=0)

    # --- required fields per document_type: field -> severity ---
    required_fields: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            "invoice": {
                "invoice_number": "error",
                "invoice_date": "error",
                "supplier_name": "error",
                "total": "error",
                "currency": "error",
                "customer_name": "warning",
                "subtotal": "warning",
                "tax": "warning",
            },
            "credit_note": {
                "invoice_number": "error",
                "invoice_date": "error",
                "supplier_name": "error",
                "total": "error",
                "currency": "error",
                "customer_name": "warning",
                "subtotal": "warning",
                "tax": "warning",
            },
            "receipt": {
                "invoice_date": "error",
                "supplier_name": "error",
                "total": "error",
                "currency": "error",
                "invoice_number": "warning",
                "customer_name": "warning",
            },
            "statement": {
                "invoice_date": "error",
                "supplier_name": "error",
                "total": "error",
                "currency": "error",
                "invoice_number": "warning",
            },
            "other": {},
        }
    )
    # Fields whose UNGROUNDED_VALUE is an error (others: warning).
    ungrounded_error_fields: list[str] = Field(default_factory=lambda: ["invoice_number", "total"])

    # --- rule registry ---
    disabled_rules: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, str] = Field(default_factory=dict)

    # --- review policy ---
    auto_approve_min: float = Field(0.85, ge=0, le=1)
    blocking_warnings: list[str] = Field(
        default_factory=lambda: ["UNGROUNDED_VALUE", "LOW_CONFIDENCE", "POSSIBLE_DUPLICATE"]
    )


@lru_cache(maxsize=1)
def get_validation_settings() -> ValidationSettings:
    return ValidationSettings()
