<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-24 22:08 UTC). -->

# Role
You are a senior backend engineer building the ingestion and parsing pipeline for a
production document-intelligence system. Every input file is untrusted. Optimize for
robustness (never crash a worker), predictable memory on huge files, measurable
performance, and clean interfaces the next stages can build on. If anything in this
spec is wrong or risky, say so before coding.

# Context
- Inputs: PDF (digital, scanned, or mixed; up to ~1,000+ pages), JPG, PNG, TIFF
  (incl. multi-page), XLSX, XLS, CSV. Thousands of files per day. Poor-quality scans
  and corrupted files are expected.
- One file can contain MANY invoices.
- Already built (Step 2): SQLAlchemy models + Alembic for documents, document_units,
  extractions, processing_jobs, processing_events, etc. Read backend/app/models first
  and reuse them. Do not redesign the schema.
- Stack: Python 3.11+, FastAPI, Celery + Redis, PostgreSQL + pgvector, SQLAlchemy 2.0,
  pydantic-settings, pytest, Docker Compose.

# Scope
IN: file intake service, storage abstraction, file-type detection, PDF/image/OCR/
spreadsheet/CSV parsing, image preprocessing, invoice boundary detection (heuristic),
Celery orchestration for these stages, a CLI for local runs, tests, benchmark, docs.
OUT: HTTP routes (Step 6), LLM extraction (Step 4), validation rules (Step 5),
embeddings/search (Step 7). Leave clean hooks for them.

# Module layout (backend/app/pipeline/)
errors.py, settings (extend app config), storage.py, filetypes.py, intake.py,
text_utils.py, pdf.py, images.py, preprocess.py, ocr/base.py, ocr/tesseract.py,
spreadsheet.py, csv_parser.py, segment.py, orchestrator.py; Celery tasks in
backend/app/workers/; CLI in backend/app/cli.py.
Parsers are PURE (bytes/path in → dataclasses out, no DB access) so they are trivially
testable. Only the orchestrator/tasks touch the database.

# Configuration (pydantic-settings, all overridable via env)
MAX_FILE_SIZE_MB=100, MAX_PAGES=2000, PAGE_BATCH_SIZE=25, OCR_DPI=300,
MIN_DIGITAL_CHARS=50, OCR_LANGS="eng", OCR_PAGE_TIMEOUT_S=60, MAX_IMAGE_PIXELS=180_000_000,
MAX_SHEET_ROWS=200_000, MAX_UNIT_TEXT_CHARS=200_000, MAX_FAILED_UNIT_RATIO=0.2,
STORE_LAYOUT=true. No magic numbers in code.

# 1. Error taxonomy (errors.py)
Two families. Retry ONLY transient ones.
- PermanentError → document fails immediately, no retry:
  UNSUPPORTED_TYPE, TYPE_MISMATCH, EMPTY_FILE, FILE_TOO_LARGE, TOO_MANY_PAGES,
  CORRUPT_FILE, PASSWORD_PROTECTED, NO_TEXT_FOUND, DECOMPRESSION_BOMB.
- TransientError → retried with exponential backoff + jitter (max 3):
  storage/DB connectivity, OCR timeout on a single page (retry that page once).
Each error carries `code`, a user-safe `message`, and internal `details`.

# 2. Storage (storage.py)
`Storage` protocol: put(stream) → key, open(key) → binary stream, local_path(key) →
context manager giving a temp file path, delete(key). Implement LocalStorage
(volume-backed). Keys are UUID-based. The user's filename NEVER becomes part of a path.

# 3. Intake (intake.py)
`ingest_file(stream, original_filename, uploaded_by, session) -> Document`, called later
by the API and now by the CLI:
- Stream to storage in chunks while computing SHA-256 and size. Enforce size limit while
  streaming, not after.
- Detect the real type from bytes (filetypes.py), never from the extension:
  - PDF magic, JPEG, PNG, TIFF (II*/MM*)
  - XLSX = ZIP containing xl/workbook.xml. Reject xlsm/xlsb/macros. Guard against zip
    bombs (total uncompressed size and ratio limits).
  - XLS = OLE2 compound file with a Workbook stream
  - CSV = decodable text that passes delimiter sniffing
  - Anything else → UNSUPPORTED_TYPE. Extension/content disagreement → TYPE_MISMATCH
    (e.g. an .exe renamed to .pdf).
- Create the documents row (status=queued) and a processing_jobs row, then enqueue.
  The DB commit happens BEFORE enqueue so the worker never races a missing row.
- Duplicate files (same sha256) are still stored. Set duplicate_of_document_id; do not
  reject.

# 4. Text hygiene (text_utils.py)
Unicode NFKC. Strip NUL bytes (\x00 — Postgres TEXT rejects them). Normalize whitespace
but KEEP line breaks (layout matters for extraction). Cap text at MAX_UNIT_TEXT_CHARS
and record truncation as a warning.

# 5. PDF (pdf.py, PyMuPDF)
- Open: if it fails → CORRUPT_FILE. Capture MuPDF repair warnings into unit warnings.
  If needs_pass, try an empty password, else PASSWORD_PROTECTED. Check page_count
  against MAX_PAGES before doing any work.
- Per page, decide the method:
  - digital if extracted text ≥ MIN_DIGITAL_CHARS AND passes a garbage check (low
    ratio of alphanumerics, "(cid:" artifacts, replacement chars → treat as broken
    font encoding → OCR)
  - else OCR. Also OCR pages where images cover >50% of the page and the text is sparse.
  - Store extraction_method per unit.
- OCR path: render with page.get_pixmap(dpi=OCR_DPI, colorspace=GRAY), one page at a
  time; free pixmaps immediately. Never hold more than one rendered page per process.
- If STORE_LAYOUT: keep line-level text + bounding boxes in structured_data["lines"]
  (the UI will later highlight evidence).
- Large PDFs: split into batches of PAGE_BATCH_SIZE as a Celery group/chord; each batch
  opens the file independently via storage.local_path. Bulk-insert units per batch.

# 6. Images (images.py)
Pillow with Image.MAX_IMAGE_PIXELS set (→ DECOMPRESSION_BOMB). verify(), then reopen.
Apply EXIF orientation (ImageOps.exif_transpose). Iterate every TIFF frame
(ImageSequence); each frame = one unit (unit_type=image_frame). Convert CMYK/palette/
alpha/16-bit to 8-bit grayscale for OCR.

# 7. Preprocessing (preprocess.py, OpenCV headless)
Measure first, then apply steps CONDITIONALLY. Blind preprocessing degrades clean images.
- Quality metrics → image_quality JSONB: width/height, estimated DPI, blur (variance of
  Laplacian), contrast, skew angle.
- Steps: orientation fix (Tesseract OSD: 0/90/180/270), upscale if text is too small /
  effective DPI < 200, denoise only when noisy, deskew only if |angle| > 0.5°, adaptive
  threshold or Otsu, border trim.
- Fallback: OCR the preprocessed image. If mean confidence < threshold, also OCR the
  original and keep the better result. Max 2 attempts per page. Record which variant won.

# 8. OCR (ocr/)
`OcrEngine` protocol: recognize(image) → OcrResult(text, mean_confidence 0–1,
lines with bbox, engine, variant, duration_ms). Implement TesseractEngine via pytesseract
image_to_data (--oem 1, psm 3, psm 6 on retry). Confidence = mean of word confidences
weighted by word length, ignoring -1. Honor OCR_PAGE_TIMEOUT_S. The engine is swappable
(PaddleOCR could be added later behind the same protocol; do not implement it now).
Set OMP_THREAD_LIMIT=1 in the worker env so concurrent Tesseract processes don't
oversubscribe CPUs.

# 9. Spreadsheets (spreadsheet.py)
- XLSX via openpyxl (read_only, data_only=True); XLS via xlrd. NEVER evaluate formulas
  or macros. If a formula cell has no cached value, record a warning, don't guess.
- Read ALL values as strings (raw), with dates converted to ISO strings. Do not let
  pandas coerce types: invoice numbers like "00123" and amounts like "1.234,56" must
  survive untouched. Numeric normalization belongs to Step 4.
- Every sheet (including hidden ones, flagged hidden) = one unit (unit_type=sheet).
- Forward-fill merged cell ranges. Drop fully empty rows/columns. Detect the header row
  (first row with mostly non-empty, mostly non-numeric cells — not always row 1).
  Detect multiple tables in one sheet separated by blank rows → table_blocks.
- structured_data = {headers, rows (list of dicts), table_blocks, hidden, n_rows, n_cols,
  warnings}. `text` = compact pipe-delimited rendering (capped) for the LLM and search.
- Enforce MAX_SHEET_ROWS.

# 10. CSV (csv_parser.py)
Detect encoding (charset-normalizer; handle UTF-8 BOM, UTF-16, cp1252). Sniff the
delimiter among , ; \t | on a sample. Read as strings. Count malformed rows and record
them as warnings instead of failing. Output has the same shape as a sheet unit
(unit_type=csv).

# 11. Invoice boundary detection (segment.py)
Pure function: list of unit texts → list of Segment(unit_start, unit_end, confidence,
reasons). Signals: "Page 1 of N" / "1/N" resets, an invoice title ("Invoice",
"Tax Invoice", "Bill") near the top of a page, a change of invoice number, a totals
block ("Total", "Amount Due", "Balance Due") closing a segment.
No signals → the whole document is one low-confidence segment.
Spreadsheets: one segment per sheet (row grouping by invoice number happens in Step 4).
Design it as a `Segmenter` protocol so Step 4 can add an LLM-assisted segmenter.

# 12. Schema additions (one small Alembic migration)
documents.segments JSONB (nullable); document_units.error_code TEXT (nullable),
document_units.warnings JSONB (default '[]'). Nothing else. If you think more is needed,
say why first.

# 13. Orchestration (orchestrator.py + workers/)
- Ordered stage list: intake → parse → segment → extract → validate → embed.
  extract/validate/embed are registered no-op stubs that log `skipped` events, so for
  now documents end `completed`. Steps 4, 5 and 7 replace the stubs.
- Each stage writes processing_events (started/succeeded/failed/skipped, duration_ms,
  details). These numbers feed the evaluation report.
- Status: queued → processing → completed | failed (error_code + message set on the
  document and the job). Set page_count, processing_started_at, processing_finished_at.
- IDEMPOTENT: re-running parse upserts on (document_id, unit_type, unit_index).
  It never duplicates.
- Partial failure: a bad page marks that unit with error_code and the job continues.
  The document fails only if failed units > MAX_FAILED_UNIT_RATIO or every unit is empty
  (NO_TEXT_FOUND).
- Celery: acks_late=True, worker_prefetch_multiplier=1, soft/hard time limits, separate
  queues `ocr` (CPU-heavy) and `default`, autoretry only for TransientError.
- Per-job temp dir, always cleaned up (finally). No shell=True anywhere.

# 14. Worker image
Dockerfile for the worker: tesseract-ocr + tesseract-ocr-eng, libmagic1,
opencv-python-headless (no libGL), pinned Python deps. Non-root user.

# 15. CLI (backend/app/cli.py)
`python -m app.cli ingest <path-or-folder> [--sync]` ingests files and, with --sync, runs
the pipeline inline. It prints a table: file, detected type, status, error_code, units,
methods (digital/ocr counts), avg OCR confidence, segments, duration.

# 16. Tests (tests/pipeline/)
Generate fixtures programmatically in conftest (PyMuPDF / Pillow / openpyxl). Don't
commit large binaries; only a tiny .xls fixture is committed. Cover:
- Digital PDF; scanned PDF (rendered page + noise + 3° rotation embedded as image);
  mixed PDF (digital + scanned pages) → correct method per page
- Encrypted PDF → PASSWORD_PROTECTED; truncated PDF → CORRUPT_FILE; zero-byte →
  EMPTY_FILE; "MZ" bytes named .pdf → TYPE_MISMATCH; oversized → FILE_TOO_LARGE
- Broken-font PDF text (cid artifacts) → falls back to OCR
- JPG with EXIF rotation; blurry PNG; 3-frame TIFF → 3 units; decompression bomb rejected
- XLSX: 2 sheets, merged cells, header on row 4, formula cell, hidden sheet; XLS fixture;
  zip-bomb XLSX rejected; xlsm rejected
- CSV: semicolon + cp1252 + BOM; leading-zero invoice numbers preserved; malformed rows
  counted as warnings
- NUL bytes stripped; text truncation warning
- Deskew estimate within ±1° on a synthetically rotated image
- Segmentation on synthetic page texts (3 invoices across 7 pages → 3 segments)
- Orchestrator: 30-page multi-invoice PDF → units, segments, and events recorded;
  running twice yields no duplicates; one corrupted page → job continues, unit flagged
OCR assertions check KEY TOKENS (e.g. "INVOICE", the invoice number), not exact text —
OCR output varies by version. Mark OCR tests @pytest.mark.ocr. They run in the worker
image.

# 17. Benchmark (scripts/bench_parse.py)
Generate a 1,000-page synthetic PDF (digital) and a 50-page scanned PDF. Report
pages/sec per method, p50/p95 per-page time, total time, and peak RSS. Write the results
to docs/benchmarks/parse.md. These are real numbers for the evaluation report.

# 18. Docs (docs/pipeline.md)
Mermaid flowchart of the pipeline. Library choices and why (PyMuPDF, Tesseract vs
Paddle, OpenCV). Thresholds and how they were chosen. Error codes table. Memory
strategy for large files. Security measures for untrusted files. Known limitations
(handwriting, tables in images lose structure, non-English needs extra tesseract packs).

# Working style
1. Before coding: restate the plan in ≤10 bullets, list assumptions, and flag anything
   you disagree with and why. Then proceed.
2. Type hints everywhere; dataclasses/pydantic for parser outputs; docstrings on public
   functions.
3. Structured logging (document_id, job_id, stage) — no print() outside the CLI.
4. Do not implement anything listed as OUT of scope.
5. Finish with: files created/changed, commands to run, benchmark results, and
   follow-ups.

# Acceptance criteria
- `pytest tests/pipeline -q` passes inside the worker container (OCR tests included).
- `python -m app.cli ingest tests/fixtures/generated --sync` processes every fixture type;
  bad files end `failed` with the correct error_code; the worker never crashes.
- 1,000-page digital PDF parses with peak RSS under ~1 GB (report the actual figure).
- Re-running a job creates no duplicate rows.
- `alembic upgrade head` and `alembic downgrade -1` both work for the new migration.--  also maintain clean code practice
