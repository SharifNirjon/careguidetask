"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-25 03:45:14.995659

Hand-reviewed after autogenerate:
- extensions (vector, pg_trgm) and trigger functions added (autogenerate can't see them)
- enum-like columns rendered as plain TEXT (CHECKs carry the allowed values)
- VECTOR(384) is hardcoded on purpose: migrations are snapshots and must not
  import app.config.EMBEDDING_DIM (see docs/database.md before changing it)
- verified: tsv generated column, partial unique/partial indexes, HNSW
  vector_cosine_ops, GIN gin_trgm_ops, DESC index columns

REVIEW CHECKLIST (autogenerate gets these wrong or misses them):
- generated columns (Computed), partial indexes (postgresql_where)
- HNSW / GIN indexes and their operator classes (vector_cosine_ops, gin_trgm_ops)
- triggers and functions (autogenerate never sees them)
- big tables: CREATE INDEX CONCURRENTLY (in an autocommit_block), CHECK ... NOT VALID + VALIDATE
- downgrade() must fully reverse upgrade()
"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SET_UPDATED_AT_FN = """
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
"""

FORBID_UPDATE_FN = """
CREATE FUNCTION forbid_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; UPDATE is not allowed', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

# corrections is append-only, with one exception: ON DELETE SET NULL on
# line_item_id (deleting a line item must not destroy its audit trail).
CORRECTIONS_APPEND_ONLY_FN = """
CREATE FUNCTION corrections_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.line_item_id IS NULL AND OLD.line_item_id IS NOT NULL
       AND (to_jsonb(NEW) - 'line_item_id') = (to_jsonb(OLD) - 'line_item_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'corrections is append-only; UPDATE is not allowed'
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # gen_random_uuid() is built in since PostgreSQL 13; no pgcrypto needed.

    op.execute(SET_UPDATED_AT_FN)
    op.execute(FORBID_UPDATE_FN)
    op.execute(CORRECTIONS_APPEND_ONLY_FN)

    op.create_table('documents',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('original_filename', sa.Text(), nullable=False),
    sa.Column('storage_key', sa.Text(), nullable=False),
    sa.Column('mime_type', sa.Text(), nullable=False),
    sa.Column('file_ext', sa.Text(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.CHAR(length=64), nullable=False),
    sa.Column('status', sa.Text(), server_default='uploaded', nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('duplicate_of_document_id', sa.UUID(), nullable=True),
    sa.Column('uploaded_by', sa.Text(), nullable=True),
    sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("error_code IN ('CORRUPT_FILE', 'PASSWORD_PROTECTED', 'UNSUPPORTED_TYPE', 'FILE_TOO_LARGE', 'NO_TEXT_FOUND', 'OCR_ERROR', 'LLM_ERROR', 'EMBEDDING_ERROR', 'TIMEOUT', 'INTERNAL_ERROR')", name=op.f('ck_documents_error_code')),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f('ck_documents_sha256_hex')),
    sa.CheckConstraint("status <> 'failed' OR error_code IS NOT NULL", name=op.f('ck_documents_failed_has_error_code')),
    sa.CheckConstraint("status IN ('uploaded', 'queued', 'processing', 'completed', 'needs_review', 'failed')", name=op.f('ck_documents_status')),
    sa.CheckConstraint('duplicate_of_document_id <> id', name=op.f('ck_documents_not_duplicate_of_self')),
    sa.CheckConstraint('page_count >= 0', name=op.f('ck_documents_page_count_nonneg')),
    sa.CheckConstraint('processing_finished_at >= processing_started_at', name=op.f('ck_documents_processing_window')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_documents_size_bytes_positive')),
    sa.ForeignKeyConstraint(['duplicate_of_document_id'], ['documents.id'], name=op.f('fk_documents_duplicate_of_document_id_documents'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_documents_storage_key'))
    )
    op.create_index(op.f('ix_documents_duplicate_of_document_id'), 'documents', ['duplicate_of_document_id'], unique=False)
    op.create_index('ix_documents_sha256', 'documents', ['sha256'], unique=False)
    op.create_index('ix_documents_status_created_at', 'documents', ['status', sa.text('created_at DESC')], unique=False)
    op.create_table('document_units',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('unit_type', sa.Text(), nullable=False),
    sa.Column('unit_index', sa.Integer(), nullable=False),
    sa.Column('sheet_name', sa.Text(), nullable=True),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('extraction_method', sa.Text(), nullable=False),
    sa.Column('ocr_confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('structured_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('image_quality', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("extraction_method IN ('digital', 'ocr', 'spreadsheet')", name=op.f('ck_document_units_extraction_method')),
    sa.CheckConstraint("unit_type IN ('pdf_page', 'image_frame', 'sheet', 'csv')", name=op.f('ck_document_units_unit_type')),
    sa.CheckConstraint('ocr_confidence >= 0 AND ocr_confidence <= 1', name=op.f('ck_document_units_ocr_confidence_range')),
    sa.CheckConstraint('unit_index >= 0', name=op.f('ck_document_units_unit_index_nonneg')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_units_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_units')),
    sa.UniqueConstraint('document_id', 'unit_type', 'unit_index', name=op.f('uq_document_units_document_id_unit_type_unit_index')),
    sa.UniqueConstraint('id', 'document_id', name=op.f('uq_document_units_id_document_id'))
    )
    op.create_table('extractions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('invoice_index', sa.Integer(), nullable=False),
    sa.Column('unit_start', sa.Integer(), nullable=False),
    sa.Column('unit_end', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('invoice_number', sa.Text(), nullable=True),
    sa.Column('invoice_date', sa.Date(), nullable=True),
    sa.Column('supplier_name', sa.Text(), nullable=True),
    sa.Column('customer_name', sa.Text(), nullable=True),
    sa.Column('subtotal', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('tax', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('currency', sa.CHAR(length=3), nullable=True),
    sa.Column('invoice_number_norm', sa.Text(), nullable=True),
    sa.Column('supplier_name_norm', sa.Text(), nullable=True),
    sa.Column('field_meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('overall_confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('model_name', sa.Text(), nullable=True),
    sa.Column('prompt_version', sa.Text(), nullable=True),
    sa.Column('raw_llm_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('review_status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('reviewed_by', sa.Text(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lock_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_extractions_currency_iso4217')),
    sa.CheckConstraint("review_status IN ('pending', 'auto_approved', 'needs_review', 'approved', 'rejected')", name=op.f('ck_extractions_review_status')),
    sa.CheckConstraint('invoice_index >= 0', name=op.f('ck_extractions_invoice_index_nonneg')),
    sa.CheckConstraint('latency_ms >= 0', name=op.f('ck_extractions_latency_ms_nonneg')),
    sa.CheckConstraint('overall_confidence >= 0 AND overall_confidence <= 1', name=op.f('ck_extractions_overall_confidence_range')),
    sa.CheckConstraint('unit_end >= unit_start', name=op.f('ck_extractions_unit_range')),
    sa.CheckConstraint('unit_start >= 0', name=op.f('ck_extractions_unit_start_nonneg')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_extractions_version_positive')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_extractions_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_extractions')),
    sa.UniqueConstraint('document_id', 'invoice_index', 'version', name=op.f('uq_extractions_document_id_invoice_index_version')),
    sa.UniqueConstraint('id', 'document_id', name=op.f('uq_extractions_id_document_id'))
    )
    op.create_index('ix_extractions_invoice_date', 'extractions', ['invoice_date'], unique=False)
    op.create_index('ix_extractions_review_status', 'extractions', ['review_status'], unique=False)
    op.create_index('ix_extractions_supplier_name_norm_invoice_number_norm', 'extractions', ['supplier_name_norm', 'invoice_number_norm'], unique=False)
    op.create_index('ix_extractions_supplier_name_norm_trgm', 'extractions', ['supplier_name_norm'], unique=False, postgresql_using='gin', postgresql_ops={'supplier_name_norm': 'gin_trgm_ops'})
    op.create_index('ix_extractions_total', 'extractions', ['total'], unique=False)
    op.create_index('uq_extractions_document_id_invoice_index_current', 'extractions', ['document_id', 'invoice_index'], unique=True, postgresql_where=sa.text('is_current'))
    op.create_table('processing_jobs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('job_type', sa.Text(), nullable=False),
    sa.Column('celery_task_id', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('attempt', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('triggered_by', sa.Text(), nullable=True),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("error_code IN ('CORRUPT_FILE', 'PASSWORD_PROTECTED', 'UNSUPPORTED_TYPE', 'FILE_TOO_LARGE', 'NO_TEXT_FOUND', 'OCR_ERROR', 'LLM_ERROR', 'EMBEDDING_ERROR', 'TIMEOUT', 'INTERNAL_ERROR')", name=op.f('ck_processing_jobs_error_code')),
    sa.CheckConstraint("job_type IN ('full', 'reprocess', 'reextract', 'revalidate', 'reembed')", name=op.f('ck_processing_jobs_job_type')),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_processing_jobs_status')),
    sa.CheckConstraint('attempt >= 1', name=op.f('ck_processing_jobs_attempt_positive')),
    sa.CheckConstraint('finished_at >= started_at', name=op.f('ck_processing_jobs_run_window')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_processing_jobs_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_processing_jobs')),
    sa.UniqueConstraint('id', 'document_id', name=op.f('uq_processing_jobs_id_document_id'))
    )
    op.create_index(op.f('ix_processing_jobs_celery_task_id'), 'processing_jobs', ['celery_task_id'], unique=False)
    op.create_index('ix_processing_jobs_document_id_queued_at', 'processing_jobs', ['document_id', sa.text('queued_at DESC')], unique=False)
    op.create_index('ix_processing_jobs_status', 'processing_jobs', ['status'], unique=False)
    op.create_table('chunks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('unit_id', sa.UUID(), nullable=False),
    sa.Column('extraction_id', sa.UUID(), nullable=True),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
    sa.Column('embedding_model', sa.Text(), nullable=True),
    sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple'::regconfig, content)", persisted=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(embedding IS NULL) = (embedding_model IS NULL)', name=op.f('ck_chunks_embedding_has_model')),
    sa.CheckConstraint('chunk_index >= 0', name=op.f('ck_chunks_chunk_index_nonneg')),
    sa.CheckConstraint('token_count >= 0', name=op.f('ck_chunks_token_count_nonneg')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_chunks_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['extraction_id', 'document_id'], ['extractions.id', 'extractions.document_id'], name=op.f('fk_chunks_extraction_id_extractions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['unit_id', 'document_id'], ['document_units.id', 'document_units.document_id'], name=op.f('fk_chunks_unit_id_document_units'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks'))
    )
    op.create_index('ix_chunks_document_id', 'chunks', ['document_id'], unique=False)
    op.create_index('ix_chunks_embedding_hnsw', 'chunks', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_with={'m': 16, 'ef_construction': 64}, postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_chunks_extraction_id', 'chunks', ['extraction_id'], unique=False)
    op.create_index('ix_chunks_tsv', 'chunks', ['tsv'], unique=False, postgresql_using='gin')
    op.create_index('ix_chunks_unit_id', 'chunks', ['unit_id'], unique=False)
    op.create_table('line_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('extraction_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('tax_rate', sa.Numeric(precision=7, scale=4), nullable=True),
    sa.Column('field_meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_line_items_position_nonneg')),
    sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id'], name=op.f('fk_line_items_extraction_id_extractions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_line_items')),
    sa.UniqueConstraint('extraction_id', 'position', name=op.f('uq_line_items_extraction_id_position'))
    )
    op.create_table('processing_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=True),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('stage', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("stage IN ('ingest', 'parse', 'ocr', 'segment', 'extract', 'validate', 'embed', 'reconcile')", name=op.f('ck_processing_events_stage')),
    sa.CheckConstraint("status IN ('started', 'succeeded', 'failed', 'skipped')", name=op.f('ck_processing_events_status')),
    sa.CheckConstraint('duration_ms >= 0', name=op.f('ck_processing_events_duration_ms_nonneg')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_processing_events_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id', 'document_id'], ['processing_jobs.id', 'processing_jobs.document_id'], name=op.f('fk_processing_events_job_id_processing_jobs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_processing_events'))
    )
    op.create_index('ix_processing_events_document_id_created_at', 'processing_events', ['document_id', 'created_at'], unique=False)
    op.create_index('ix_processing_events_job_id', 'processing_events', ['job_id'], unique=False)
    op.create_table('reconciliations',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('left_extraction_id', sa.UUID(), nullable=False),
    sa.Column('right_extraction_id', sa.UUID(), nullable=False),
    sa.Column('match_key', sa.Text(), nullable=False),
    sa.Column('match_method', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('differences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("match_method IN ('invoice_number', 'fuzzy')", name=op.f('ck_reconciliations_match_method')),
    sa.CheckConstraint("status IN ('matched', 'mismatched')", name=op.f('ck_reconciliations_status')),
    sa.CheckConstraint('left_extraction_id < right_extraction_id', name=op.f('ck_reconciliations_canonical_pair_order')),
    sa.ForeignKeyConstraint(['left_extraction_id'], ['extractions.id'], name=op.f('fk_reconciliations_left_extraction_id_extractions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['right_extraction_id'], ['extractions.id'], name=op.f('fk_reconciliations_right_extraction_id_extractions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliations')),
    sa.UniqueConstraint('left_extraction_id', 'right_extraction_id', name=op.f('uq_reconciliations_left_extraction_id_right_extraction_id'))
    )
    op.create_index(op.f('ix_reconciliations_right_extraction_id'), 'reconciliations', ['right_extraction_id'], unique=False)
    op.create_table('validation_issues',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('extraction_id', sa.UUID(), nullable=False),
    sa.Column('rule_code', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('field_path', sa.Text(), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('expected_value', sa.Text(), nullable=True),
    sa.Column('actual_value', sa.Text(), nullable=True),
    sa.Column('related_extraction_id', sa.UUID(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by', sa.Text(), nullable=True),
    sa.Column('resolution_note', sa.Text(), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("rule_code IN ('MISSING_FIELD', 'TOTAL_MISMATCH', 'LINE_SUM_MISMATCH', 'DUPLICATE_INVOICE', 'LOW_CONFIDENCE', 'UNGROUNDED_VALUE', 'CROSS_DOC_MISMATCH', 'INVALID_DATE', 'INVALID_CURRENCY', 'INVALID_FORMAT')", name=op.f('ck_validation_issues_rule_code')),
    sa.CheckConstraint("severity IN ('error', 'warning', 'info')", name=op.f('ck_validation_issues_severity')),
    sa.CheckConstraint('related_extraction_id <> extraction_id', name=op.f('ck_validation_issues_related_not_self')),
    sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id'], name=op.f('fk_validation_issues_extraction_id_extractions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['related_extraction_id'], ['extractions.id'], name=op.f('fk_validation_issues_related_extraction_id_extractions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_validation_issues'))
    )
    op.create_index(op.f('ix_validation_issues_extraction_id'), 'validation_issues', ['extraction_id'], unique=False)
    op.create_index('ix_validation_issues_extraction_id_active', 'validation_issues', ['extraction_id'], unique=False, postgresql_where=sa.text('superseded_at IS NULL'))
    op.create_index(op.f('ix_validation_issues_related_extraction_id'), 'validation_issues', ['related_extraction_id'], unique=False)
    op.create_index('ix_validation_issues_rule_code_severity', 'validation_issues', ['rule_code', 'severity'], unique=False)
    op.create_table('corrections',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('extraction_id', sa.UUID(), nullable=False),
    sa.Column('line_item_id', sa.UUID(), nullable=True),
    sa.Column('field_path', sa.Text(), nullable=False),
    sa.Column('old_value', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('new_value', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('corrected_by', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id'], name=op.f('fk_corrections_extraction_id_extractions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['line_item_id'], ['line_items.id'], name=op.f('fk_corrections_line_item_id_line_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_corrections'))
    )
    op.create_index(op.f('ix_corrections_extraction_id'), 'corrections', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_corrections_line_item_id'), 'corrections', ['line_item_id'], unique=False)

    for table in ("documents", "extractions"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
    op.execute(
        "CREATE TRIGGER trg_processing_events_append_only BEFORE UPDATE ON processing_events "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update()"
    )
    op.execute(
        "CREATE TRIGGER trg_corrections_append_only BEFORE UPDATE ON corrections "
        "FOR EACH ROW EXECUTE FUNCTION corrections_append_only()"
    )


def downgrade() -> None:
    # Dropping a table drops its indexes, constraints and triggers with it.
    for table in (
        "corrections",
        "validation_issues",
        "reconciliations",
        "processing_events",
        "line_items",
        "chunks",
        "processing_jobs",
        "extractions",
        "document_units",
        "documents",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS corrections_append_only()")
    op.execute("DROP FUNCTION IF EXISTS forbid_update()")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
