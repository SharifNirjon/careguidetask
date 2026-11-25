# Requirement → test matrix

Audit of the suite at the start of Step 9 (commit `fdaed89` plus the uncommitted
Step 8 work). Status: ✅ covered, ⚠ partial or weak, ❌ no test yet (a gap that
Step 9 closes; the gap list is at the end).

Types: **U** unit (pure, no I/O) · **I** integration (PostgreSQL / Redis) ·
**O** OCR (needs Tesseract) · **E** end to end (full stack) · **L** live LLM.

Paths are relative to `tests/` unless they start with `frontend/`.

## 1. Document processing (intake and pipeline)

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Real type from content, not the extension | `pipeline/test_intake.py::test_detects_real_type`, `::test_extension_content_disagreement_is_type_mismatch`, `::test_unknown_extension_trusts_content`, `api/test_documents.py::test_renamed_exe_is_type_mismatch` | U, I | ✅ |
| Bad files rejected with a code (EMPTY_FILE, UNSUPPORTED_TYPE, TYPE_MISMATCH, macros) | `pipeline/test_intake.py::test_rejects_bad_files`, `::test_random_binary_is_unsupported`, `::test_macro_content_is_rejected_even_with_xlsx_extension` | U | ✅ |
| Size limit enforced while streaming (FILE_TOO_LARGE, 413) | `pipeline/test_intake.py::test_size_limit_is_enforced_while_streaming`, `api/test_documents.py::test_oversized_file_is_413_and_ingests_nothing` | I | ✅ |
| Rejections leave nothing behind | `pipeline/test_intake.py::test_intake_rejections_leave_nothing_behind` | I | ✅ |
| Duplicates stored and linked (DUPLICATE_FILE) | `pipeline/test_intake.py::test_duplicate_is_stored_and_linked`, `db/test_constraints.py::test_duplicate_uploads_are_stored_and_flagged`, `api/test_documents.py::test_duplicate_upload_links_original` | I | ✅ |
| Two identical uploads at the same time | none | I | ❌ |
| Digital PDF text layer, layout | `pipeline/test_pdf.py::test_digital_pdf_uses_text_layer_with_layout`, `::test_layout_can_be_disabled` | U | ✅ |
| Encrypted PDF (PASSWORD_PROTECTED) | `pipeline/test_pdf.py::test_encrypted_pdf` | U | ✅ |
| Corrupt / truncated / repaired PDF (CORRUPT_FILE) | `pipeline/test_pdf.py::test_garbage_bytes_are_corrupt`, `::test_truncated_pdf_is_corrupt`, `::test_repaired_pdf_is_flagged`, `pipeline/test_orchestrator.py::test_truncated_pdfs_end_corrupt` | U, I | ✅ |
| Page limit before parsing (TOO_MANY_PAGES) | `pipeline/test_pdf.py::test_too_many_pages_checked_before_parsing` | U | ✅ |
| Multi-invoice PDF segmentation | `pipeline/test_segment.py` (6 tests), `pipeline/test_orchestrator.py::test_30_page_multi_invoice_pdf_with_one_bad_page`, `extraction/test_service.py::test_multi_segment_pdf_gives_one_versioned_extraction_per_segment` | U, I | ✅ |
| Unit failure tolerance / NO_TEXT_FOUND | `pipeline/test_orchestrator.py::test_too_many_failed_units_fails_document`, `::test_all_units_empty_is_no_text_found` | I | ✅ |
| Idempotent re-run (no duplicates) | `pipeline/test_orchestrator.py::test_rerunning_creates_no_duplicates`, `::test_failure_is_idempotent`, `extraction/test_service.py::test_retrying_the_same_job_creates_nothing_and_spends_nothing` | I | ✅ |
| Text cleanup (NUL, control chars, NFKC) | `pipeline/test_text_utils.py` (5 tests), `pipeline/test_tables.py::test_nul_bytes_in_csv_are_stripped` | U | ✅ |
| NUL bytes inside a PDF text layer | none | U | ❌ |
| Index stage never fails a document | `pipeline/test_index_stage.py` (2 tests) | I | ✅ |
| Worker routing, autoretry only transient, deferral | `pipeline/test_workers.py` (6 tests) | U | ✅ |
| Worker crash: task redelivered (acks_late) → idempotent, no duplicates | `pipeline/test_workers.py::test_tasks_skip_jobs_that_already_finished` (partial: finished jobs only) | U | ⚠ |
| Task time limit → sweeper WORKER_TIMEOUT | `api/test_jobs_ops.py::test_sweeper` | I | ✅ |
| DB error mid-stage → rollback, retry, no partial rows | none | I | ❌ |
| Storage write failure (STORAGE_ERROR) | none | I | ❌ |

## 2. OCR

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Scanned PDF OCR'd; per-page method in mixed PDFs | `pipeline/test_pdf.py::test_scanned_pdf_is_ocred`, `::test_mixed_pdf_method_per_page`, `::test_blank_page_goes_to_ocr`, `::test_broken_font_text_layer_falls_back_to_ocr` | O | ✅ |
| Deskew, upscale, border trim, no blind preprocessing | `pipeline/test_preprocess.py` (7 tests) | O | ✅ |
| Rotated / upside-down pages | `pipeline/test_preprocess.py::test_upside_down_and_sideways_pages_are_reoriented`, `::test_upside_down_page_is_fixed_by_the_low_confidence_retry`, `pipeline/test_images.py::test_exif_rotated_jpeg_ocr` | O | ✅ |
| Images: EXIF, modes, alpha, TIFF frames, blur | `pipeline/test_images.py` (10 tests) | U, O | ✅ |
| Decompression bomb (DECOMPRESSION_BOMB) | `pipeline/test_images.py::test_decompression_bomb_rejected_from_header`, `::test_bomb_between_1x_and_2x_limit_is_still_rejected` | U | ✅ |
| Invalid / truncated image | `pipeline/test_images.py::test_truncated_image_is_corrupt` | U | ✅ (TIFF not specifically) |
| OCR engine failure (OCR_ERROR) | none | U | ❌ |

## 3. Spreadsheet parsing

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Every sheet a unit; header detection; merged cells; multiple tables | `pipeline/test_tables.py` (13 tests) | U | ✅ |
| XLS, CSV encodings and delimiters, malformed rows | `pipeline/test_tables.py::test_xls_fixture`, `::test_csv_encodings_and_delimiters`, `::test_malformed_rows_are_counted_not_fatal` | U | ✅ |
| Uncached formulas warned, never guessed | `pipeline/test_tables.py::test_uncached_formula_is_a_warning_not_a_guess` | U | ✅ |
| Zip bomb XLSX / broken XLSX zip | fixture `zip_bomb.xlsx` used in `pipeline/test_intake.py::test_rejects_bad_files`; broken zip not covered | U | ⚠ |
| Deterministic tabular extraction (no LLM) | `extraction/test_tabular.py` (6 tests), `extraction/test_service.py::test_clean_spreadsheet_costs_zero_llm_requests` | U, I | ✅ |

## 4. AI extraction and the LLM gateway

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Prompt golden file; injection stays inside the document block | `extraction/test_prompts_inputs.py` (10 tests) | U | ✅ |
| Strict schema, temperature 0, output-mode ladder | `llm/test_chat_gateway.py::test_happy_path_sends_temperature_zero_and_strict_schema`, `llm/test_structured.py` (10 tests) | U | ✅ |
| 429 with Retry-After | `llm/test_chat_gateway.py::test_429_retry_after_is_honored` | U | ✅ |
| 5xx backoff then fallback / recovery | `llm/test_chat_gateway.py::test_5xx_backs_off_then_falls_back`, `::test_circuit_breaker_opens_and_closes` | U | ✅ |
| Timeout | none (transport timeout not simulated) | U | ❌ |
| Malformed JSON repaired once, then parse error | `llm/test_chat_gateway.py::test_malformed_json_is_repaired_once`, `::test_failed_repair_raises_parse_error` | U | ✅ |
| Hallucinated values caught by grounding (UNGROUNDED_VALUE) | `extraction/test_grounding.py` (10 tests), `extraction/test_assemble.py::test_fabricated_invoice_number_is_nulled_and_recorded` | U | ✅ |
| All models down (LLM_UNAVAILABLE, transient) | `llm/test_chat_gateway.py::test_all_models_down_raises_unavailable`, `extraction/test_service.py::test_all_models_down_is_transient` | U, I | ✅ |
| Quota exhausted → deferred (LLM_QUOTA_DEFERRED) | `extraction/test_service.py::test_quota_exhaustion_defers_instead_of_failing`, `pipeline/test_workers.py::test_quota_deferral_reschedules_after_the_reset`, `llm/test_budget.py` (6 tests) | U, I | ✅ |
| Auth error is permanent | `llm/test_chat_gateway.py::test_auth_errors_are_permanent` | U | ✅ (gateway only, not the document end state) |
| API key never logged | `llm/test_chat_gateway.py::test_api_key_never_logged`, `::test_missing_api_key_fails_before_any_request` | U | ✅ |
| Normalizers (amounts, dates, currency, names) | `extraction/test_normalize.py` (11 tests) | U | ✅ (examples only; no property tests) |
| Confidence calculation | `extraction/test_grounding.py::test_field_confidence_is_the_weakest_signal`, `::test_overall_confidence_weights_required_fields`, `extraction/test_assemble.py::test_ocr_confidence_caps_field_confidence` | U | ✅ |
| Live smoke test | `llm/test_live.py` (2 tests) | L | ✅ (opt-in) |

## 5. Validation (rules and cross-document checks)

Every rule code has at least one test in `validation/test_rules.py` (32 tests) or
`validation/test_crossdoc.py` / `test_service_db.py`, except as marked.

| Rule code | Tests | Status |
| --- | --- | --- |
| MISSING_FIELD | `test_rules.py::test_missing_field_severity_by_document_type` | ✅ |
| UNGROUNDED_VALUE | `test_rules.py::test_ungrounded_severity`, `::test_ungrounded_line_item_value` | ✅ |
| LOW_CONFIDENCE | `test_rules.py::test_low_field_confidence_boundary`, `::test_low_overall_confidence_boundary` | ✅ |
| NOT_AN_INVOICE | `test_rules.py::test_not_an_invoice_skips_required_fields` | ✅ |
| INVALID_AMOUNT | `test_rules.py::test_invalid_amount` | ✅ |
| NEGATIVE_AMOUNT, SIGN_INCONSISTENT | `test_rules.py::test_negative_amount_only_on_non_credit_notes`, `::test_credit_note_signs` | ✅ |
| IMPLAUSIBLE_TAX_RATE | `test_rules.py::test_implausible_tax_rate_boundary` | ✅ |
| ZERO_TOTAL | `test_rules.py::test_zero_total` | ✅ |
| TOTAL_MISMATCH | `test_rules.py::test_total_mismatch_message_shows_the_math`, `::test_total_mismatch_tolerance_boundary`, `::test_total_beyond_tolerance_is_always_flagged` (hypothesis) | ✅ |
| LINE_AMOUNT_MISMATCH, LINE_SUM_MISMATCH | `test_rules.py::test_line_amount_mismatch_per_line`, `::test_line_sum_mismatch` | ✅ |
| TAX_INCLUSIVE_LINES | `test_rules.py::test_tax_inclusive_lines_are_info_not_error` | ✅ |
| INVALID_DATE, FUTURE_DATE, DATE_TOO_OLD | `test_rules.py::test_invalid_date`, `::test_future_date_grace`, `::test_date_too_old_boundary` | ✅ |
| UNKNOWN_CURRENCY | `test_rules.py::test_unknown_currency` | ✅ |
| INVALID_CURRENCY, INVALID_FORMAT | never raised: legacy enum values kept for the DB check constraint (UNKNOWN_CURRENCY and INVOICE_NUMBER_FORMAT replaced them). A test will pin that no rule emits them. | ⚠ |
| INVOICE_NUMBER_FORMAT | `test_rules.py::test_invoice_number_format` | ✅ |
| DUPLICATE_INVOICE, POSSIBLE_DUPLICATE | `test_crossdoc.py::test_later_copy_gets_duplicate_invoice_error`, `::test_fuzzy_match_is_only_a_warning`, `test_service_db.py::test_exact_duplicate_across_two_pdfs` | ✅ |
| INVOICE_NUMBER_CONFLICT | `test_service_db.py::test_same_number_different_amounts_conflicts_on_both` | ✅ |
| CROSS_DOC_VERIFIED, CROSS_DOC_MISMATCH | `test_service_db.py::test_pdf_vs_spreadsheet_verified_and_mismatched`, `test_crossdoc.py::test_cross_doc_mismatch_lists_diffs_in_message_and_expected` | ✅ |
| Tolerance and money arithmetic | `validation/test_money.py` (7 tests, hypothesis), `test_rules.py::test_consistent_invoices_have_no_arithmetic_issues` (hypothesis) | ✅ |
| Issue lifecycle (supersede, keep resolution, reopen) | `validation/test_policy_lifecycle.py` (7 tests), `test_service_db.py` (lifecycle tests) | U, I | ✅ |
| Planted-error fixtures | `validation/test_planted.py` (4 tests) | U | ✅ |
| Reconciliation buckets and runs | `validation/test_reconciliation.py` (6 tests), `api/test_jobs_ops.py::test_reconciliation_run_lifecycle` | U, I | ✅ |

## 6. Search

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Chunking (lines, overlap, sheets, summary) | `search/test_chunking.py` (12 tests; `test_invariants` uses hypothesis) | U | ✅ |
| Keyword: code splitting identical in Python and SQL, no stemming, boost | `search/test_keyword.py` (8 tests), `db/test_search.py` (7 tests) | U, I | ✅ |
| Semantic, hybrid RRF, space isolation, degradation | `search/test_semantic_hybrid.py` (12 tests), `api/test_search_api.py` | I | ✅ (RRF ordering has examples, not properties) |
| Embedding stage: batching, quota share, unchanged chunks, deferral | `search/test_embedding.py` (8 tests) | I | ✅ |
| Embedding failure never fails a document (EMBEDDING_ERROR is a legacy value, never set by design) | `pipeline/test_index_stage.py::test_indexing_failure_never_fails_the_document` | I | ✅ |
| Snippets are text segments, never HTML | `search/test_snippets.py::test_script_content_comes_back_as_plain_text`, `api/test_search_api.py::test_snippets_are_segments_not_html`, `frontend/src/features/search/search.test.tsx` | U, I | ✅ |

## 7. API endpoints (26 routes)

Every route is called by at least one test in `tests/api/`; the snapshot test
`test_openapi_contract.py::test_every_route_is_documented` fails on an
undocumented route.

| Endpoint | Main tests | Status |
| --- | --- | --- |
| `GET /api/v1/documents` | `test_documents.py::test_filters`, `::test_sorting`, `::test_keyset_pagination_is_stable_across_inserts`, `::test_invalid_cursor_is_422` | ✅ |
| `POST /api/v1/documents` | `test_documents.py::test_mixed_upload_gives_per_file_outcomes`, `::test_idempotency_key_replay`, `::test_too_many_files_is_400`, `::test_enqueue_failure_still_202` | ✅ (no_files, idempotency_in_progress ❌) |
| `DELETE /api/v1/documents/{id}` | `test_documents.py::test_soft_delete_is_admin_only` | ✅ |
| `GET /api/v1/documents/{id}` | `test_documents.py::test_detail_progress_and_etag`, `::test_unknown_document_is_404` | ✅ |
| `GET …/events` | `test_documents.py::test_events_are_paginated` | ✅ |
| `GET …/file` | `test_documents.py::test_file_download_headers`, `test_units.py::test_content_disposition_cannot_inject_headers` | ✅ |
| `POST …/reprocess` | `test_jobs_ops.py::test_reprocess_creates_and_enqueues`, `::test_second_reprocess_is_409`, `::test_concurrent_reprocess_only_one_wins` | ✅ |
| `GET …/results` | `test_documents.py::test_results_shape_and_etag` | ✅ |
| `GET …/units/{n}/image` | `test_documents.py::test_page_image_renders_and_caches`, `::test_page_image_dpi_bounds`, `::test_image_frame_preview` | ✅ |
| `GET …/units/{n}/table` | `test_documents.py::test_sheet_unit_table`, `::test_page_unit_has_no_table` | ✅ |
| `GET /api/v1/extractions/{id}` | `test_corrections_review.py` (indirectly) | ⚠ (extraction_not_found ❌) |
| `PATCH /api/v1/extractions/{id}` | `test_corrections_review.py` (10 tests: 412, 428, 422, line ops) | ✅ |
| `POST …/approve`, `…/reject` | `test_corrections_review.py::test_approve_with_open_errors_is_409_then_resolve_then_approve`, `::test_reject_requires_note` | ✅ |
| `GET …/versions`, `…/corrections` | `test_corrections_review.py` | ✅ |
| `POST /api/v1/issues/{id}/resolve`, `…/reopen` | `test_corrections_review.py::test_resolving_needs_note_and_unknown_issue_is_404`, `::test_reopen_restores_blocking` | ✅ (not_resolved ❌) |
| `POST /api/v1/jobs/reprocess-failed` | `test_jobs_ops.py::test_bulk_reprocess_skips_permanent_errors`, `::test_bulk_limit_is_bounded` | ✅ |
| `GET /api/v1/jobs/{id}` | `test_jobs_ops.py` | ⚠ (job_not_found ❌) |
| `POST /api/v1/reconciliations`, `GET …/{id}` | `test_jobs_ops.py::test_reconciliation_run_lifecycle`, `::test_reconciliation_rejects_unknown_documents` | ⚠ (run_not_found ❌) |
| `GET /api/v1/search` | `test_search_api.py` (11 tests) | ✅ (query_too_long ❌) |
| `GET /api/v1/stats` | `test_jobs_ops.py::test_stats` | ✅ |
| `GET /health/live`, `/health/ready` | `test_platform.py::test_health_needs_no_auth`, `::test_ready_is_503_when_db_down` | ✅ |

### API error codes

Tested: version_mismatch, unit_not_found, not_current, invalid_values,
document_not_found, validation_error, unresolved_errors, unknown_documents,
too_many_files, request_too_large, rate_limited, offset_out_of_range, job_active,
issue_superseded, invalid_idempotency_key, invalid_cursor, internal_error,
if_match_required, forbidden, file_too_large.

**Untested (❌):** extraction_not_found, unauthorized (the 401 tests check the
status, not the code), run_not_found, query_too_long, not_resolved, no_files,
job_not_found, issue_not_found, idempotency_in_progress, dpi_out_of_range.

## 8. Platform, security and authorization

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| 401 for no / unknown / revoked key | `api/test_platform.py::test_no_key_is_401_problem`, `::test_unknown_or_malformed_key_is_401`, `::test_revoked_key_is_401` | I | ✅ |
| Role checks | `api/test_platform.py::test_viewer_cannot_write`, `::test_admin_only_routes` (hand-picked routes) | I | ⚠ (no route × role matrix; a new route is not caught) |
| problem+json everywhere, generic 500 | `api/test_platform.py::test_422_is_problem_json_with_field_errors`, `::test_unhandled_exception_is_generic_500` | I | ✅ |
| CORS allowlist, security headers, body limit | `api/test_platform.py` | I | ✅ |
| Rate limit 429 + Retry-After | `api/test_platform.py::test_rate_limit_429_with_retry_after`, `api/test_search_api.py::test_search_has_its_own_stricter_rate_limit` | I | ✅ |
| Redis unavailable (rate limiter) | none: behavior undefined today | I | ❌ |
| Audit fields from the principal | `api/test_platform.py::test_audit_fields_come_from_principal` | I | ✅ |
| Header injection via filenames | `api/test_units.py::test_content_disposition_cannot_inject_headers` | U | ✅ |
| Path traversal in filenames | none | I | ❌ |
| Oversized headers and params | none (params: `test_search_api.py::test_query_bounds` only) | I | ⚠ |
| SQL-injection strings in every filter / search param | none | I | ❌ |
| API key never in logs or responses (API side) | none (LLM side ✅) | I | ❌ |
| Static / dependency / secret scans | `detect-secrets` pre-commit only; no bandit, pip-audit, npm audit, gitleaks | — | ❌ |

## 9. Data layer

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Migrations round trip and match models | `db/test_migrations.py` (8 tests) | I | ✅ |
| Constraints, enums, append-only tables, cascades | `db/test_constraints.py` (28 tests) | I | ✅ |
| Versioning and locking | `db/test_versioning.py` (4 tests) | I | ⚠ (`test_concurrent_reprocessing_is_serialized` sleeps 0.5 s) |
| Keyset pagination under inserts | `api/test_documents.py::test_keyset_pagination_is_stable_across_inserts` (one fixed scenario) | I | ⚠ (no property test) |
| Concurrent corrections → one 412 | `db/test_versioning.py::test_optimistic_locking_detects_concurrent_review` (service layer, not HTTP) | I | ⚠ |

## 10. Contract

| Requirement | Tests | Status |
| --- | --- | --- |
| openapi.json snapshot; money never a number | `api/test_openapi_contract.py` (4 tests) | ✅ |
| Frontend types in sync with openapi.json | `frontend: npm run check:api` (script, not part of a test run) | ⚠ |

## 11. Frontend

| Requirement | Tests | Type | Status |
| --- | --- | --- | --- |
| Money / dates / decimal formatting | `frontend/src/lib/format/format.test.ts` | U | ✅ |
| Error mapping (AppError, request id) | `frontend/src/lib/api/errors.test.ts` | U | ✅ |
| BFF proxy allowlist, origin check, header filtering | `frontend/src/lib/server/proxy.test.ts` | U | ✅ |
| Review form: dirty-only patch, 412, 422, hints | `frontend/src/features/review/schemas.test.ts`, `review-panel.test.tsx` | U | ✅ |
| Upload: progress, idempotency key per batch | `frontend/src/features/upload/upload.test.tsx` | U | ✅ |
| Search debounce, degraded banner, snippets | `frontend/src/features/search/search.test.tsx` | U | ✅ |
| Full keyboard review flow, axe, no direct backend calls | `frontend/e2e/review-flow.spec.ts` (2 tests) | E | ✅ |
| API key absent from the client bundle | `frontend: npm run check:bundle` (script) | — | ✅ |
| Feature coverage ≥ 75 % | not measured | — | ❌ |

## Baseline (before Step 9)

`pytest --cov=backend/app --cov-branch` on the audit commit: **773 passed, 10
skipped** (OCR tests, which need Tesseract, and the opt-in live tests) in
150 s, serial. Branch coverage: **89 % overall**; per package see
[../test-results/summary.md](../test-results/summary.md) (generated from the same
run). Every Step 9 gate is met there; the weak spots are the workers (65.6 %) and
the top-level CLI/config modules (41.3 %).

## Gap list

**Missing tests**

1. Failure scenarios with no explicit test: concurrent identical uploads, DB error
   mid-stage, storage write failure (STORAGE_ERROR), OCR engine failure
   (OCR_ERROR), LLM timeout, auth error → document end state, Redis down,
   redelivered running task, broken XLSX zip, invalid TIFF, NUL bytes in a PDF
   text layer, HTTP-level concurrent corrections.
2. Dead enum values (INVALID_CURRENCY, INVALID_FORMAT, EMBEDDING_ERROR): pin that nothing emits them, document them as legacy.
3. Ten API error codes never asserted (listed in §7).
4. No route × role authorization matrix.
5. Security: path traversal, SQL-injection strings, oversized headers, API key in
   API logs/responses, plus all static scans.
6. Property tests for normalizer round trips, RRF ordering, keyset pagination.
7. Load test, coverage gates (pytest-cov is not installed), CI, the test report.

**Weak tests**

- `db/test_versioning.py::test_concurrent_reprocessing_is_serialized` sleeps 0.5 s
  to "prove" the second worker is blocked: replace with polling `pg_locks` for a
  waiting backend.
- Network blocking exists only in `tests/llm` and `tests/extraction`; the rest of
  the suite can reach the network.
- Three files read the real clock (`api/test_jobs_ops.py`, `search/test_embedding.py`,
  `validation/test_service_db.py`); to review whether that can flake near midnight
  UTC (the quota day).
- No tests without assertions were found (AST scan).

**Structure**

- Markers: only `ocr` and `live` are registered; no `--strict-markers`; no
  unit/integration split, so "the unit suite in under 60 s" can't be run today.
- Fixtures: `tests/fixtures/factory.py` plus ad-hoc builders in
  `tests/validation/builders.py`, `tests/api/seed.py`, `tests/db/factories.py`;
  no ground truth JSON; no shared generator for the Step 10 evaluation.
- No parallelism (one session database).
