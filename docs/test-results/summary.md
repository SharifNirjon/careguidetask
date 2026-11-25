# Test results

Generated 2026-11-25 17:44 UTC by `scripts/test_report.py` from real test output. Do not edit by hand.

- Commit: `fdaed89` (plus uncommitted changes)
- Host: Windows-10-10.0.26200-SP0, Python 3.11.9
- Backend log: saved run finished 2026-11-25 16:42 UTC (`--backend-log`)

## Suites

| Suite | Passed | Failed | Skipped | Duration |
|---|---:|---:|---:|---:|
| Backend (unit + integration, PostgreSQL + Redis) | 773 | 0 | 10 | 150 s |
| Evaluation metrics (pure) | 13 | 0 | 0 | 0 s |
| Frontend unit (Vitest) | 98 | 0 | 0 | 7 s |
| Frontend e2e (Playwright) | not run | | | |

Skipped backend tests: OCR tests need the Tesseract binary (they run in the worker image),
and live LLM tests run only with `RUN_LIVE_LLM=1` (they spend real quota).

## Backend branch coverage

Overall is pytest-cov's TOTAL; packages are size-weighted from the per-file percentages
(coverage.py rounds those to whole percent, so a package value can be off by up to 0.5).

| Package | Coverage | Gate | Met |
|---|---:|---:|:---:|
| (overall) | 89.0 % | 85 % | yes |
| (top-level modules) | 41.3 % |  |  |
| api | 94.4 % | 85 % | yes |
| db | 80.0 % |  |  |
| extraction | 97.1 % | 85 % | yes |
| extraction/normalize.py | 98.0 % | 95 % | yes |
| llm | 92.3 % |  |  |
| models | 96.9 % |  |  |
| pipeline | 86.5 % | 85 % | yes |
| search | 94.0 % |  |  |
| validation | 95.7 % | 95 % | yes |
| workers | 65.6 % |  |  |

## Slowest backend tests

| Time | Phase | Test |
|---:|---|---|
| 3.90 s | setup | `tests/api/test_corrections_review.py::test_fixing_total_supersedes_mismatch` |
| 3.89 s | call | `tests/llm/test_embedding_gateway.py::test_inputs_are_batched_one_request_per_batch` |
| 3.47 s | call | `tests/db/test_migrations.py::test_upgrade_downgrade_upgrade_round_trip` |
| 2.77 s | call | `tests/llm/test_chat_gateway.py::test_happy_path_sends_temperature_zero_and_strict_schema` |
| 2.48 s | teardown | `tests/validation/test_service_db.py::test_supplier_legal_suffix_variants_match` |

## Files

- `backend-pytest.log`: the full backend run (pytest + coverage output).
- `eval-junit.xml`, `frontend-junit.xml`: JUnit XML of the other suites.

What each test covers: [../testing/matrix.md](../testing/matrix.md).
