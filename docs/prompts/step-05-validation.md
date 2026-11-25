<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-25 00:50 UTC). -->

# Role
You are a senior backend engineer building the validation layer of a production
invoice-processing system. Validation decides what a human must review, so it must be
precise (few false alarms), explainable (every issue says exactly what's wrong and by how
much), and deterministic. If anything here is wrong or risky, say so before coding.

# Context
- Steps 2–4 are done. Read backend/app/models, backend/app/extraction and the
  orchestrator first. Follow CLAUDE.md (code quality + security standards).
- Each current extraction has:
  - normalized fields (Decimal amounts, ISO date, ISO currency)
  - match keys (supplier_name_norm, invoice_number_norm)
  - field_meta per field (confidence, grounded, evidence, source, rejected_value)
  - line_items, document_type, overall_confidence
- Tables to use: validation_issues, reconciliations, extractions.review_status,
  documents.status.
- The orchestrator has a no-op `validate` stage stub. Replace it.

# Scope
IN: rule engine, rule catalog, duplicate detection, cross-document verification
(PDF↔PDF, PDF↔Excel), batch reconciliation, review decision policy, issue lifecycle,
ValidationService (used by the pipeline now and by manual corrections in Step 6),
migration, tests, docs.
OUT: HTTP routes (Step 6), UI (Step 8).

# 1. Architecture (backend/app/validation/)
- ValidationContext (frozen dataclass) holds:
  - the extraction snapshot and line items
  - document info (source kind: document | spreadsheet; document_type)
  - currency minor units and "today"
  - PRE-FETCHED candidate matches for the duplicate/cross-doc checks
- ValidationRule protocol: `code`, `default_severity`, `applies(ctx) -> bool`,
  `check(ctx) -> list[Issue]`. Rules are PURE (no DB, no clock, no network). All I/O
  happens before the rules run (CandidateFinder) and after (IssueRepository).
- Explicit ordered rule registry, with per-rule enable/disable and severity overrides
  from settings. No auto-discovery magic.
- Issue (dataclass): rule_code, severity (error | warning | info), field_path, message,
  expected_value, actual_value, related_extraction_id, fingerprint.

# 2. Money arithmetic
- Decimal only. Quantize to the currency's ISO 4217 minor units: JPY/KRW 0,
  BHD/KWD/OMR 3, default 2. Keep this as a small explicit table in code.
- Tolerance:
  - abs_tol = minor_unit × (1 + number_of_lines), to absorb per-line rounding
  - rel_tol = VALIDATION_REL_TOL (default 0.005) × the larger amount
  - It's a mismatch only if the difference exceeds max(abs_tol, rel_tol).
  One function, heavily tested.
- Validation MAY compute expected values (for messages and suggestions). It NEVER writes
  them into extraction fields.

# 3. Rule catalog

Completeness & trust
- MISSING_FIELD: required fields per document_type come from settings.
  - Invoice: invoice_number, invoice_date, supplier_name, total, currency = error;
    customer_name, subtotal, tax = warning.
  - Receipt: invoice_number and customer_name are warnings only.
- UNGROUNDED_VALUE: field_meta has a rejected_value. Error for invoice_number and total,
  warning otherwise. The message shows the rejected value.
- LOW_CONFIDENCE: field confidence < FIELD_CONFIDENCE_MIN, or overall confidence <
  OVERALL_CONFIDENCE_MIN (warning).
- NOT_AN_INVOICE: document_type is "other" (warning; routes to review).

Numeric validity
- INVALID_AMOUNT: a raw value is present but normalization produced null (error).
- NEGATIVE_AMOUNT: negative total or subtotal on a non-credit-note (error).
- SIGN_INCONSISTENT: a credit note with mixed signs across subtotal/tax/total (warning).
- IMPLAUSIBLE_TAX_RATE: tax/subtotal < 0 or > MAX_TAX_RATE (default 0.5) (warning).
- ZERO_TOTAL: total == 0 (warning).

Arithmetic consistency
- LINE_AMOUNT_MISMATCH: quantity × unit_price ≠ amount, per line (warning; field_path
  "line_items[i].amount").
- LINE_SUM_MISMATCH: Σ line amounts ≠ subtotal (error).
  - Recognize tax-inclusive invoices: if Σ lines ≈ total and subtotal + tax ≈ total,
    emit an info issue TAX_INCLUSIVE_LINES instead.
  - If subtotal is missing, compare Σ lines with total − tax and say so in the message.
  - Skip when there are no line items.
- TOTAL_MISMATCH: subtotal + tax ≠ total (error).
- Messages show the math, e.g. "Subtotal 100.00 + tax 15.00 = 115.00, but total is
  120.00 (difference 5.00)". expected_value carries the computed value, so the UI can
  offer it as a suggestion.

Dates & formats
- INVALID_DATE: a raw date is present but normalized to null (error).
- FUTURE_DATE: invoice_date > today + FUTURE_DATE_GRACE_DAYS (error).
- DATE_TOO_OLD: older than MAX_INVOICE_AGE_YEARS (warning).
- UNKNOWN_CURRENCY: not a valid ISO 4217 code (error).
- INVOICE_NUMBER_FORMAT: normalized key under 2 or over 40 chars, or no alphanumerics
  (warning).

Duplicates & cross-document (see §4)
- DUPLICATE_FILE (info), DUPLICATE_INVOICE (error), POSSIBLE_DUPLICATE (warning),
  INVOICE_NUMBER_CONFLICT (error), CROSS_DOC_VERIFIED (info), CROSS_DOC_MISMATCH (error).

# 4. Duplicates & cross-document verification
CandidateFinder (a repository, runs BEFORE the rules) finds other CURRENT extractions
from OTHER documents. It excludes versions of the same document and soft-deleted
documents.
- Exact match: same invoice_number_norm AND supplier similarity ≥ SUPPLIER_MATCH_MIN
  (pg_trgm similarity on supplier_name_norm, default 0.8).
- Fuzzy match: supplier similarity ≥ threshold AND same total AND invoice_date within
  ±DUPLICATE_DATE_WINDOW_DAYS (default 3), with a different or missing invoice number.
- Uses the Step 2 indexes. Verify with EXPLAIN that there are no sequential scans on
  large tables.

Classify an exact match with a pure, table-tested function:
| Source kinds          | Key fields equal? | Result                                   |
|-----------------------|-------------------|------------------------------------------|
| same (doc↔doc)        | yes               | DUPLICATE_INVOICE on the LATER one       |
| same (doc↔doc)        | no                | INVOICE_NUMBER_CONFLICT on both          |
| different (doc↔sheet) | yes               | CROSS_DOC_VERIFIED (info) on both        |
| different (doc↔sheet) | no                | CROSS_DOC_MISMATCH on both, diffs listed |

- Key fields: total, subtotal, tax, currency, invoice_date, customer name (normalized
  with the Step 4 function), line count, and Σ line amounts (with the §2 tolerance).
- Every comparison upserts a reconciliations row: ordered pair, status, differences
  [{field, left, right}].
- "Later" means the later documents.created_at (tie-break by id). related_extraction_id
  points to the counterpart.
- Fuzzy matches only ever produce POSSIBLE_DUPLICATE (warning), never an error.
- Concurrency: two copies processed at the same time must not miss each other. Wrap
  find-and-write in pg_advisory_xact_lock(hashtext(invoice_number_norm)), and document
  why.
- When B matches A, re-run the duplicate/cross-doc rules for A too, so both sides get
  their issues.

Batch reconciliation (a user explicitly asks to check PDFs against an Excel ledger, or
PDFs against PDFs):
- ReconciliationService.run(left_document_ids, right_document_ids, actor) matches current
  extractions across the two sets.
- The report lists: matched, mismatched (with diffs), only_in_left, only_in_right, and
  ambiguous (one-to-many matches).
- Stored as a reconciliation_runs row (summary JSONB), with reconciliations linked by
  run_id. Runs as a Celery task; Step 6 exposes it.

# 5. Review decision policy (a pure function)
- Any unresolved error → review_status = needs_review.
- Only warnings/info → auto_approved if overall_confidence ≥ AUTO_APPROVE_MIN and no
  warning is in BLOCKING_WARNINGS (default: UNGROUNDED_VALUE, LOW_CONFIDENCE,
  POSSIBLE_DUPLICATE). Otherwise needs_review.
- Never overwrite a human decision: approved/rejected stay as they are, unless a
  correction changes a field (then recompute).
- Document status: needs_review if any current extraction needs review, otherwise
  completed. (failed is owned by the earlier stages.)

# 6. Issue lifecycle (fingerprint diff, so a reviewer's work is never lost)
- fingerprint = sha256(rule_code, field_path, related_extraction_id).
- On each run, compute the new set and compare it with the active issues
  (superseded_at IS NULL):
  - unchanged fingerprint → keep the row (its id and resolution state are preserved)
  - new fingerprint → insert
  - gone → set superseded_at (fixed by a correction or a re-extraction)
- A resolved issue (a reviewer acknowledged it with a note) no longer blocks approval.
  If the same issue reappears on a NEW extraction version, it counts as a new issue.
- All of this happens in one transaction, together with the review_status update.

# 7. ValidationService & orchestrator
- ValidationService.validate_extraction(extraction_id, *, trigger: pipeline | correction
  | counterpart | manual) → ValidationOutcome (issues, review_status, duration_ms).
  Target < 50 ms, excluding DB I/O.
- Replace the `validate` stub: validate every current extraction of the document,
  update the document status, and write processing_events (counts by severity and rule).
- Expose revalidate_document(document_id) for Step 6.

# 8. Migration (one, small)
- validation_issues.fingerprint TEXT NOT NULL, plus a partial index on (extraction_id,
  fingerprint) WHERE superseded_at IS NULL.
- reconciliation_runs (id, left_document_ids UUID[], right_document_ids UUID[], status,
  summary JSONB, created_by, created_at, finished_at), and reconciliations.run_id as a
  nullable FK.
- Nothing else. Justify anything extra first.

# 9. Tests (tests/validation/)
- Every rule: pass, fail and boundary cases (parametrized). Include:
  - a difference exactly at the tolerance
  - 20 lines with per-line rounding
  - JPY (0 decimals) and KWD (3 decimals)
  - tax-inclusive lines
  - missing subtotal with line items present
  - credit-note negatives
  - future date within the grace period
  - receipt vs invoice required fields
- Property-based (hypothesis): generated consistent invoices produce no arithmetic
  issues; pushing the total beyond tolerance always yields TOTAL_MISMATCH.
- The duplicate/cross-doc classification table: all 4 rows, plus fuzzy.
- DB integration (real Postgres):
  - an exact duplicate across two PDFs is flagged
  - "ACME Ltd." matches "Acme Limited"
  - versions of the same document never match each other
  - PDF vs spreadsheet: one verified case, one mismatched case with diffs
  - concurrency: two sessions validating two copies at once still flag the duplicate
- Lifecycle: a resolved issue survives re-validation; correcting the value supersedes the
  issue; review_status follows the policy; approved stays approved.
- Batch reconciliation report covers matched, mismatched, only_in_left, only_in_right
  and ambiguous.
- Planted-error fixtures: expose a helper that returns the detected rule codes per
  fixture, so Step 10 can compute validation precision/recall.

# 10. Docs (docs/validation.md)
- Rule catalog table (code, severity, condition, example message).
- Tolerance rationale and the decision policy.
- Duplicate vs cross-doc classification table.
- Lifecycle diagram (Mermaid) and batch reconciliation.
- Configuration reference.
- Known limitations (e.g. discounts and shipping lines can cause LINE_SUM_MISMATCH false
  positives).

# Working style
1. Before coding: restate the plan in ≤10 bullets, list assumptions, flag disagreements.
2. Build in this order: money/tolerance → rules → decision policy → lifecycle →
   candidate finder → cross-doc → service/orchestrator → batch reconciliation → docs.
3. Finish with: files created/changed, commands to run, follow-ups.

# Acceptance criteria
- `pytest tests/validation -q` passes; ruff, `ruff format --check` and mypy (strict for
  app/validation) pass.
- On the planted-error fixtures, every planted error is flagged with the expected rule
  code, and the clean fixtures get no errors.
- Re-validating an unchanged extraction changes no rows (idempotent).
- `alembic upgrade head` / `alembic downgrade -1` work.
