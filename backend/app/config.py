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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dimension of the sentence-embedding model (all-MiniLM-L6-v2 -> 384).
# Changing this value requires: a new Alembic migration that alters
# chunks.embedding to VECTOR(<new dim>) (dropping/recreating the HNSW index),
# and a full re-embed of every chunk. tests/db/test_migrations.py fails if the
# database and this constant disagree.
EMBEDDING_DIM: Final[int] = 384
EMBEDDING_MODEL_DEFAULT: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"


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

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_pipeline_settings() -> PipelineSettings:
    return PipelineSettings()
