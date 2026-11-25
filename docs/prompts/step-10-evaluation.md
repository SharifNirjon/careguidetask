<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-25 16:43 UTC). -->

# Role
You are a senior ML/evaluation engineer. Produce an honest, reproducible evaluation of
the document-intelligence system with ACTUAL measured metrics (extraction accuracy,
processing time, document success rate, failure rate), plus the analysis a reviewer needs
to trust them. Honest numbers beat impressive ones. If anything here is wrong or risky,
say so before coding.

# Context
- Steps 2–9 are done. The synthetic generator lives in tools/synth/ (seeded, emits ground
  truth). Read docs/extraction.md, docs/validation.md and docs/search.md. Follow
  CLAUDE.md.
- LLM: OpenRouter free tier (~20 requests/min; daily cap of 1,000 with credits, 50
  without). Each invoice segment costs ≥ 1 request, spreadsheets cost 0 (rule-based), and
  embeddings cost batched requests. The evaluation must fit the quota and survive a quota
  cut mid-run.

# Golden rule
Never write a metric by hand. Every number in the report is rendered from the
metrics.json that the evaluation code produces. Write the narrative analysis only from
those numbers and the error list. Where human judgment is needed, leave clearly marked
placeholders instead of inventing conclusions.

# 1. Dataset (eval/dataset/)
Generate with tools/synth/ using a fixed seed. Target composition (adjust and justify if
the quota requires it):
| Category | Count | Purpose |
|---|---|---|
| Digital PDFs, single invoice, ≥ 5 layouts (classic, modern, minimal, table-heavy, European number format, tax-inclusive, receipt, credit note) | 30 | baseline accuracy |
| Scanned PDFs at mild/medium/heavy degradation (rotation, blur, noise, JPEG quality, low DPI) | 20 | OCR robustness |
| Images: JPG/PNG/TIFF, incl. multi-frame TIFF and mild perspective warp | 15 | image pipeline |
| XLSX/XLS: multi-sheet, header offset, merged cells, several invoices per sheet | 10 | spreadsheet parsing |
| CSV with different delimiters/encodings | 10 | CSV parsing |
| Multi-invoice PDFs (5–10 invoices each) | 5 | segmentation |
| Planted validation errors: wrong total, line-sum mismatch, missing invoice no., future date, duplicate pair, PDF↔Excel mismatch pair, PDF↔PDF conflict | 20 | validation precision/recall |
| Corrupt / unsupported / password-protected | 8 | failure handling |
- Vary the fields realistically: missing customer, no tax, ambiguous dates, $ with no
  currency code, long line-item tables.
- Optional real data: a small subset of a public receipts dataset (e.g. SROIE), scored
  only on overlapping fields (supplier, date, total). Check its license, and provide a
  download script instead of committing the files.
- Throughput-only file: the 1,000-page PDF from Step 3, used for parse timing with
  extraction disabled (so it doesn't burn quota).
- Split: dev (30 %) for tuning, test (70 %) for the headline numbers. Never tune prompts
  or thresholds on the test split.
- manifest.jsonl, validated by a Pydantic model: {file, sha256, split, format, category,
  layout, degradation, expected: {invoices: [...fields, line_items]},
  expected_error_code?, expected_rule_codes?, notes}.
- Commit the dataset if it's under ~50 MB; otherwise publish it as a release asset and
  verify the hashes.

# 2. Runner (eval/run_eval.py)
- Runs the REAL system end to end through the API (upload → poll until terminal state)
  on the running docker compose stack.
- Records the environment: git SHA, date, CPU/RAM, worker concurrency, OCR engine
  version, the model IDs actually used, prompt version, and any settings that affect
  results.
- Before starting: estimate the LLM and embedding requests needed, compare with the
  remaining quota (llm-check), and abort with a clear message if it isn't enough.
- Checkpointing: per-file results are appended to predictions.jsonl, and `--resume`
  continues after a quota cut. Flags: `--split dev|test`, `--limit`, `--category`.
- Official runs use `--no-cache` (no response cache) so timings are honest. Record the
  cache hit rate either way.
- Collect from the DB/API: final status, error_code, extractions with field_meta, issues,
  processing_events (stage timings), LLM tokens, requests and retries.

# 3. Matching predictions to ground truth
- Multi-invoice files: match predicted invoices to expected ones by invoice_number_norm;
  otherwise use optimal assignment (Hungarian, scipy) on field similarity. Unmatched
  predictions are spurious segments; unmatched ground truth is missed invoices. Report
  segmentation precision and recall.
- Line items: optimal assignment on description similarity + amount.

# 4. Metrics (eval/metrics.py — pure, unit-tested)
Define every metric precisely in the report.

Extraction (per field: invoice_number, invoice_date, supplier_name, customer_name,
subtotal, tax, total, currency)
- Accuracy: exact match after normalization (amounts equal at 2 dp / minor units, dates
  exact, strings normalized). Plus a secondary lenient score for names (fuzzy ≥ 0.9).
- Precision (when we output a value, how often is it right), recall (of the fields
  present in ground truth, how many we got right), and missing rate.
- Hallucination rate: a non-null prediction where the ground truth is null. Report it
  prominently, since the assignment says never invent values.
- Invoice-level exact match: all required fields correct.
- Line items: precision/recall/F1 over lines; per-line accuracy of quantity, unit_price
  and amount.
- Breakdowns: by format, category, layout, degradation level, and digital vs OCR.

Confidence & automation
- Calibration: accuracy per confidence bucket (reliability table + ECE).
- Automation trade-off: for auto-approve thresholds from 0.5 to 0.95, show the share of
  invoices auto-approved and how many of those were fully correct. Mark the configured
  threshold.

Validation
- Per rule: precision and recall on the planted errors.
- False positives on clean documents: issues per clean document, and the % of clean
  documents sent to review.

Processing time
- Per document and per page: p50/p90/p95/max, by format.
- Stage breakdown: queue wait, parse, OCR, extract, validate, embed.
- LLM latency p50/p95; tokens and requests per document.
- Batch throughput (pages/min, docs/min); the 1,000-page parse time and peak memory.

Reliability (define these exactly)
- Document success rate = valid documents that reached completed or needs_review ÷ all
  valid documents.
- Failure rate = valid documents that ended failed (or never reached a terminal state) ÷
  all valid documents.
- Expected-failure handling = invalid files that failed with the correct error_code ÷
  all invalid files. Report it separately: correctly rejected files are not "failures".
- Also report retries, quota deferrals, and runs that needed --resume.

Search (small, labelled set)
- 20 queries with known relevant documents: recall@5, recall@10 and MRR for keyword,
  semantic and hybrid, plus latency per mode.

Statistics
- Report n for every number, and 95 % Wilson confidence intervals for proportions.
  Don't present a breakdown cell with n < 5 as a percentage without flagging it.

# 5. Free ablation: grounding
Using rejected_value from field_meta (no extra LLM calls), compute what accuracy and the
hallucination rate would have been WITHOUT the grounding check. This shows what grounding
contributes.

# 6. Error analysis
- errors.csv lists every wrong, missing or hallucinated field: file, field, expected,
  predicted, raw value, evidence, confidence, unit_index, method (digital / OCR).
- Auto-categorize where possible (OCR misread, normalization, wrong label, segmentation,
  hallucination), and leave a column for manual categorization.
- The report includes the category table (counts) and 3–5 concrete examples, each with
  what would fix it.

# 7. Outputs
- Each run writes eval/results/<timestamp>_<sha>/ containing: environment.json,
  predictions.jsonl, per_doc.csv, metrics.json, errors.csv, and charts/*.png
  (matplotlib: accuracy by field, accuracy by degradation, latency distribution, stage
  breakdown, calibration, automation trade-off).
- eval/report.py renders docs/EVALUATION.md from metrics.json using a template with these
  sections:
  1. Summary: the headline table (test split), with CIs
  2. Setup: dataset composition, environment, models, prompt version, commit
  3. Metric definitions
  4. Results: fields, invoices, line items, breakdowns, validation, processing time,
     reliability, search, calibration and automation
  5. Grounding ablation
  6. Error analysis
  7. Dev → test history: what changed after the dev runs (from a changelog file)
  8. Limitations and threats to validity: synthetic data bias, small n, free-tier latency
     variability, a single machine, OCR engine version
  9. Next steps

# 8. Tests (tests/eval/)
Metrics on tiny hand-built cases where the exact numbers are known: field accuracy,
hallucination rate, precision/recall, line-item F1, Hungarian matching, Wilson intervals,
ECE, percentiles, and the success/failure definitions. Also manifest validation and the
resume logic.

# 9. Reproducibility
`make eval-dev`, `make eval` (test split) and `make eval-report`. Document how to
reproduce a run in docs/EVALUATION.md, including the expected quota usage and runtime.

# Working style
1. Before coding: restate the plan in ≤10 bullets, list assumptions, flag disagreements.
2. Order: manifest + dataset → metrics (with tests) → runner → dev run → analysis → fixes
   (logged in the changelog) → final test run → report.
3. Finish with: the headline table, where the system is weakest, the quota used, and
   follow-ups.

# Acceptance criteria
- `pytest tests/eval -q` passes.
- A full test-split run completes (with --resume if needed), and docs/EVALUATION.md is
  generated entirely from metrics.json.
- The report states field accuracy, hallucination rate, processing time (p50/p95),
  document success rate, failure rate and expected-failure handling, each with n and CI.
- Re-running `make eval-report` on the same results produces an identical report.
