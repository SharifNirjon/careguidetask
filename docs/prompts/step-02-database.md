<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-24 21:38 UTC). -->

# Role
You are a senior backend engineer designing the persistence layer for a production
document-intelligence system. Optimize for correctness, auditability, and query
performance at scale. If any part of this spec is wrong or risky, say so before coding.

# System context
- Ingests invoices/receipts as PDF (digital or scanned, up to ~1,000 pages), JPG/PNG/TIFF,
  XLSX/XLS, and CSV. Thousands of documents per day.
- Pipeline: upload → parse/OCR → LLM extraction → validation → embedding → search.
- A single file can contain MANY invoices.
- Humans review flagged results and correct values manually.
- Required capabilities: full processing history, duplicate detection, PDF↔PDF and
  PDF↔Excel cross-verification, keyword + semantic search.

# Stack (already set up in the repo)
Python 3.11+, FastAPI, Celery + Redis, PostgreSQL 16 + pgvector, SQLAlchemy 2.0
(typed `Mapped[]` style), Alembic, psycopg 3, pytest.
The docker-compose `db` service must use the `pgvector/pgvector:pg16` image — update it if not.

# Scope
ONLY the database layer: models, migrations, session setup, docs, tests, seed script.
Do NOT build API routes, Celery tasks, or extraction/validation logic.

# Global conventions
- Primary keys: UUID, server default `gen_random_uuid()`. Exception: high-volume
  append-only tables use BIGINT identity (noted below).
- All timestamps `timestamptz`. `created_at` default now(); `updated_at` maintained on update.
- Money and quantities: NUMERIC(18,4). NEVER float. Rates: NUMERIC(7,4).
  Confidence: NUMERIC(4,3) with CHECK between 0 and 1.
- Enum-like columns: TEXT + CHECK constraint (not native Postgres ENUM — ALTER TYPE is
  painful to migrate). Mirror each as a Python `StrEnum`.
- Set a naming convention on MetaData so constraint/index names are deterministic
  (pk_, fk_, uq_, ix_, ck_).
- Every foreign key is indexed. ON DELETE CASCADE from documents downward.
  Documents themselves are soft-deleted (`deleted_at`).
- Table names: plural snake_case.

# Tables

1. documents — one uploaded file
   id, original_filename, storage_key (UNIQUE), mime_type (detected from bytes), file_ext,
   size_bytes (CHECK > 0), sha256 CHAR(64), status
   (uploaded|queued|processing|completed|needs_review|failed), error_code (nullable:
   CORRUPT_FILE, PASSWORD_PROTECTED, UNSUPPORTED_TYPE, NO_TEXT_FOUND, LLM_ERROR, ...),
   error_message, page_count, duplicate_of_document_id (self-FK, nullable), uploaded_by,
   processing_started_at, processing_finished_at, created_at, updated_at, deleted_at.
   Indexes: sha256 (NON-unique — duplicates are stored and flagged, never rejected),
   (status, created_at DESC).

2. document_units — smallest parsed unit: a PDF page, an image/TIFF frame, or a sheet
   id, document_id, unit_type (pdf_page|image_frame|sheet|csv), unit_index (0-based,
   CHECK >= 0), sheet_name (nullable), text, extraction_method (digital|ocr|spreadsheet),
   ocr_confidence (nullable), structured_data JSONB (normalized rows for sheets),
   image_quality JSONB (dpi, skew, blur score), created_at.
   UNIQUE (document_id, unit_type, unit_index).

3. extractions — one invoice found in a document, one row per extraction run (versioned)
   id, document_id, invoice_index, unit_start, unit_end (CHECK unit_end >= unit_start),
   version INT (starts at 1), is_current BOOL,
   Fields: invoice_number, invoice_date DATE, supplier_name, customer_name, subtotal, tax,
   total, currency CHAR(3) CHECK (currency ~ '^[A-Z]{3}$'), all nullable — missing values
   are NULL, never guessed.
   Match keys (filled by app code): invoice_number_norm, supplier_name_norm.
   field_meta JSONB — per field: {confidence, evidence_text, unit_index,
   source: llm|rule|manual, grounded: bool}.
   overall_confidence, model_name, prompt_version, raw_llm_response JSONB,
   token_usage JSONB, latency_ms,
   review_status (pending|auto_approved|needs_review|approved|rejected), reviewed_by,
   reviewed_at, lock_version INT (SQLAlchemy `version_id_col` — optimistic locking so two
   reviewers can't silently overwrite each other), created_at, updated_at.
   Constraints:
   - Partial UNIQUE (document_id, invoice_index) WHERE is_current
   - UNIQUE (document_id, invoice_index, version)
   - NO CHECK total >= 0 — credit notes can be negative.
   Indexes: (supplier_name_norm, invoice_number_norm) for duplicate detection;
   GIN trigram (pg_trgm) on supplier_name_norm for fuzzy matching; invoice_date; total;
   review_status.
   Rule: reprocessing inserts a new version and flips is_current in the SAME transaction.

4. line_items
   id, extraction_id, position INT, description, quantity, unit_price, amount,
   tax_rate (nullable), field_meta JSONB. UNIQUE (extraction_id, position).

5. validation_issues
   id, extraction_id, rule_code (MISSING_FIELD, TOTAL_MISMATCH, LINE_SUM_MISMATCH,
   DUPLICATE_INVOICE, LOW_CONFIDENCE, UNGROUNDED_VALUE, CROSS_DOC_MISMATCH, ...),
   severity (error|warning|info), field_path (e.g. "line_items[2].amount"), message,
   expected_value TEXT, actual_value TEXT, related_extraction_id (nullable FK — for
   duplicates/cross-doc), resolved_at, resolved_by, resolution_note,
   superseded_at (set when re-validation replaces it — never delete history), created_at.
   Indexes: partial on extraction_id WHERE superseded_at IS NULL; (rule_code, severity).

6. corrections — append-only audit of manual edits
   id, extraction_id, line_item_id (nullable), field_path, old_value JSONB,
   new_value JSONB, corrected_by, reason, created_at.

7. processing_jobs — one row per attempt to process a document
   id, document_id, job_type (full|reprocess|reextract|revalidate|reembed),
   celery_task_id, status (queued|running|succeeded|failed|cancelled), attempt INT,
   triggered_by, error_code, error_message, queued_at, started_at, finished_at.
   Indexes: (document_id, queued_at DESC), status.

8. processing_events — append-only per-stage log (the "processing history")
   id BIGINT identity, job_id, document_id (denormalized for fast lookup),
   stage (ingest|parse|ocr|segment|extract|validate|embed|reconcile),
   status (started|succeeded|failed|skipped), duration_ms, details JSONB, created_at.
   Index: (document_id, created_at).

9. reconciliations — cross-document verification (PDF↔PDF, PDF↔Excel)
   id, left_extraction_id, right_extraction_id, match_key,
   match_method (invoice_number|fuzzy), status (matched|mismatched),
   differences JSONB ([{field, left, right}]), created_at.
   CHECK (left_extraction_id < right_extraction_id) so A–B and B–A can't both exist;
   UNIQUE (left_extraction_id, right_extraction_id).

10. chunks — search index
    id, document_id, unit_id (FK document_units), extraction_id (nullable), chunk_index,
    content, token_count, embedding VECTOR(EMBEDDING_DIM), embedding_model,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED, created_at.
    Indexes: GIN(tsv); HNSW (embedding vector_cosine_ops); document_id.
    EMBEDDING_DIM is ONE constant in config (default 384, all-MiniLM-L6-v2). Changing it
    requires a migration — document this. Use 'simple' FTS config (invoices contain
    codes, names, mixed languages; stemming hurts more than it helps) — note the trade-off.

# Migrations
- Alembic reads DATABASE_URL from env; compare_type=True.
- Initial migration enables extensions: vector, pg_trgm.
- Autogenerate, then HAND-REVIEW: generated columns, partial indexes, HNSW/GIN indexes,
  and trigram operator classes must be explicit and correct.
- Fully reversible: downgrade drops everything cleanly. No data in migrations.

# DB module (backend/app/db/)
- session.py: engine with pool_pre_ping, pool size from env, statement_timeout (30s) via
  connect args; sessionmaker; FastAPI dependency `get_db()`; context manager for Celery.
- Use SYNC SQLAlchemy (psycopg 3) for both API and workers: Celery is sync, FastAPI sync
  endpoints run in a threadpool, one code path is simpler to test. Explain this in docs.

# Docs: docs/database.md
- Mermaid `erDiagram` covering all tables and relationships.
- One paragraph per table on its purpose.
- Design decisions and trade-offs: UUIDs; TEXT+CHECK vs ENUM; versioned extractions +
  is_current; append-only audit tables; pgvector inside Postgres vs a separate vector DB;
  'simple' FTS config; why duplicates are stored not rejected; scale plan (partition
  processing_events and chunks by month when volume demands — not implemented now).
- Example SQL: keyword search, semantic search, hybrid search (reciprocal rank fusion),
  find duplicate invoices, fetch a document with current extractions + open issues.

# Tests: tests/db/
Run against REAL Postgres with pgvector (docker-compose db or testcontainers — no SQLite).
Each test runs
