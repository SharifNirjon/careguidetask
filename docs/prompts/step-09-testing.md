<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-25 16:37 UTC). -->

# Role
You are a senior engineer who owns test strategy and quality for a production
document-intelligence system. Most features already have tests from their own steps.
Your job: audit those tests against the requirements, close the gaps, make the suite
fast and trustworthy, add failure-scenario and security testing, set up CI, and produce
test results a reviewer can trust. If anything here is wrong or risky, say so before
coding.

# Context
- Steps 2–8 are done, with tests under tests/ (pipeline, llm, extraction, validation,
  search, api) and frontend tests (Vitest, Playwright). Follow CLAUDE.md.
- The assignment requires automated tests for document processing, OCR, spreadsheet
  parsing, AI extraction, validation, API endpoints and failure scenarios, plus the test
  results as a deliverable.

# Scope
IN: test audit + requirement matrix, gap filling, markers and layout, a shared synthetic
document generator, the failure-scenario suite, an authorization matrix, property-based
tests, security scans, a load test, coverage gates, CI, test-report generation, docs.
OUT: new product features. If a test exposes a bug, fix it minimally and list it.

# 1. Audit first
- Inventory every test and map it to requirements in docs/testing/matrix.md:
  requirement → test files/names → type (unit / integration / e2e) → status. Cover each
  task area above, plus every error code, rule code and endpoint.
- List gaps, weak tests (no meaningful assertion, sleeping, order- or network-dependent)
  and duplicates. Fix or delete them.

# 2. Suite organization
- Markers: unit (pure, no I/O), integration (Postgres/Redis), ocr (needs Tesseract),
  live (real LLM; opt-in via RUN_LIVE_LLM=1), e2e (full stack), slow. Register them in
  pyproject with strict markers.
- A plain `pytest` runs unit + integration. The unit suite runs in under 60 s.
- Run in parallel with pytest-xdist: one database per worker (cloned from a template DB)
  and a transaction rollback per test.
- Determinism: fixed seeds, frozen time, no sleep() (use polling helpers with timeouts),
  network blocked except where marked.
- Shared synthetic document generator (tools/synth/): seeded and deterministic, used by
  the tests AND by the Step 10 evaluation dataset.
  - Invoice templates → digital PDFs, degraded scans, images, XLSX/XLS/CSV, multi-invoice
    PDFs, broken files, with ground-truth JSON emitted alongside.
  - Tests import it instead of building ad-hoc fixtures.

# 3. Failure-scenario suite (tests/failures/)
One explicit test per scenario. Inject faults through dependency injection (FaultyStorage,
FlakyGateway, a broken DB session) rather than mocking internals.
- Files: truncated PDF, broken XLSX zip, invalid TIFF, zero-byte file, password-protected
  PDF, wrong extension, oversized file, decompression bomb, zip bomb, blank scanned page,
  90°-rotated scan, NUL bytes in text.
- LLM: timeout; 429 with Retry-After; 500 then recovery; malformed JSON; schema-valid
  but hallucinated values (caught by grounding); all models down; quota exhausted
  (deferred, not failed); auth error (permanent).
- Infrastructure:
  - DB error mid-stage → rollback, job retry, no partial rows
  - Redis unavailable → define and test the behavior explicitly (including whether rate
    limiting fails open or closed); responses are problem+json
  - storage write failure
  - worker crash, simulated by task redelivery (acks_late → idempotent re-run, no
    duplicates)
  - task time limit → the sweeper marks WORKER_TIMEOUT
- Concurrency: two identical uploads at once (both stored, duplicate flagged);
  concurrent corrections (one gets 412); concurrent reprocess (409).
- Every test asserts the final document/job state, the error_code, the events written,
  and that no exception escapes the worker.

# 4. Authorization matrix
A table-driven test that iterates over app.routes, so any new route without an explicit
entry FAILS the test. For each route × role (anonymous, revoked key, viewer, reviewer,
admin), assert the expected status code.

# 5. Property-based tests (hypothesis)
- Normalizers: format random Decimals in several locale styles, then parse them back to
  equal values.
- Tolerance and arithmetic rules: consistent invoices → no issues; perturbation beyond
  the tolerance → an issue.
- Chunker invariants; RRF ordering invariants; keyset pagination returns every row
  exactly once under random inserts.

# 6. Contract tests
- openapi.json snapshot (it exists already; keep it meaningful).
- Frontend generated types stay in sync with openapi.json (the test fails if stale).

# 7. Security testing
- Static and dependency scans: bandit (Python), pip-audit and npm audit (fail on high
  severity), gitleaks over the full git history, ruff security rules (S).
- Tests for:
  - path traversal in filenames
  - oversized headers and params
  - SQL-injection strings in every filter and search param (safe results, no 500)
  - XSS payloads in document text are never rendered as HTML (snippet segments)
  - the API key never appears in logs or responses
- Only if time permits: an OWASP ZAP baseline scan against the running API, with the
  report saved.

# 8. Performance & load
- A k6 (or Locust) script: 50 virtual users for 2 minutes against list, detail, results
  and keyword search, plus a low rate of uploads. Report p50/p95/p99 latency, throughput
  and error rate in docs/benchmarks/load.md.
- Use seeded data only, with the fake gateway configured: no live LLM calls during the
  load test.
- Reference the parse benchmark (Step 3) and the search benchmark (Step 7).

# 9. Coverage
- pytest-cov with branch coverage. Gates:
  - overall ≥ 85 %
  - app/validation and app/extraction/normalize.py ≥ 95 %
  - app/extraction, app/pipeline and app/api ≥ 85 %
  - frontend features/ (Vitest) ≥ 75 %
- Don't game coverage: no tests that just execute code without asserting anything.
- Only if time permits: mutation testing (mutmut) on normalize.py and the tolerance
  module, with the mutation score reported.

# 10. CI (.github/workflows/ci.yml)
- Jobs, in order: lint + typecheck (ruff, mypy, eslint, tsc) → backend unit +
  integration (services: pgvector/pgvector:pg16, redis) → OCR tests in the worker image →
  frontend unit → e2e (docker compose + Playwright) → security scans.
- Cache dependencies. Upload artifacts: JUnit XML, coverage XML/HTML, Playwright report,
  security reports. Add a status badge to the README.
- CI must pass on a clean clone with no secrets (live tests skipped).

# 11. Test results deliverable
- `make test-report` runs every suite except live and writes docs/test-results/:
  - summary.md (generated and committed): passed/failed/skipped per suite, durations,
    coverage per package, commit SHA, date, environment
  - junit.xml and coverage.xml
  Large HTML reports go to CI artifacts, not git.
- `make test-live` (opt-in) runs the live LLM smoke test and appends its results,
  including the quota used.
- Every number in summary.md is generated by the script. Never typed by hand.

# 12. Developer ergonomics
Makefile targets: test, test-unit, test-integration, test-ocr, test-e2e, test-live,
test-failures, coverage, test-report, lint, typecheck, security, load-test.

# 13. Docs (docs/testing.md)
Strategy (the test pyramid and why), markers, how to run each suite, fixtures and the
synthetic generator, the fault-injection approach, the CI pipeline, coverage gates, how
to add a test, known gaps.

# Working style
1. Start with the audit and the matrix. Share the gap list before writing new tests.
2. Then: suite organization → generator → failure suite → authz matrix → property tests →
   security → load → coverage gates → CI → report → docs.
3. Finish with: the matrix, the closed gap list, bugs found and fixed, and the final
   summary.md.

# Acceptance criteria
- `make test` and `make test-failures` are green, and the coverage gates are met.
- The authz matrix covers every route (adding an unlisted route fails the test).
- docs/testing/matrix.md maps every requirement to at least one test.
- CI passes on a clean clone, and docs/test-results/summary.md is generated with real
  numbers.
