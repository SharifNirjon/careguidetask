# Parse benchmark

Generated 2026-11-24 23:03 UTC by `scripts/bench_parse.py`.

Each file is parsed in a fresh process the way a worker does it (batches of 25 pages, file reopened per batch, OCR at 300 DPI). Pages/sec is per method over the time spent on those pages; peak RSS is the child process's `ru_maxrss` (generation excluded).

| file | pages | method | n | pages/sec | p50 ms | p95 ms | total s | peak RSS MB | failed |
|---|---|---|---|---|---|---|---|---|---|
| digital | 1000 | digital | 1000 | 1555.21 | 0 | 3 | 1.4 | 396 | 0 |
| scanned | 50 | ocr | 50 | 0.28 | 3352 | 5290 | 181.1 | 667 | 0 |

Environment:

- CPUs visible: 8, single worker process, `OMP_THREAD_LIMIT=1`
- CPython 3.11.11 on Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.36
- PyMuPDF 2.1, tesseract 5.3.0

Throughput scales roughly linearly with worker processes on the `ocr` queue (one Tesseract process per worker, no shared state).

Peak RSS is the Python worker process only; Tesseract runs as a short-lived subprocess per page and is not included.
