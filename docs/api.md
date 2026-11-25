# REST API

The contract for the frontend and integrations. Base path `/api/v1`. The
machine-readable contract is [`docs/openapi.json`](openapi.json), a reviewed snapshot that
CI keeps in sync with the code. Interactive docs are at `/docs` when `DOCS_ENABLED=true`.
Ready-to-run requests are in [`docs/api.http`](api.http).

```
routers/       thin: parse -> call a service -> map to a response
services/      use cases and transactions; raise domain errors, never HTTP ones
repositories/  SQL (SQLAlchemy expressions only; no string-built SQL)
schemas/       request/response models, separate from the ORM (ORM objects never leave services)
problems.py    the one place that maps domain errors to HTTP
```

## Running it

```bash
docker compose up -d --build                    # db, redis, worker, beat, api
docker compose exec -w /app/backend api alembic upgrade head
docker compose exec api python -m app.cli api-key create --name demo --role reviewer
curl -H "Authorization: Bearer cg_..." http://localhost:8000/api/v1/documents
python scripts/e2e_api.py --create-key          # full walkthrough
```

The server is gunicorn with uvicorn workers. `WEB_CONCURRENCY` sets the worker
count and `DB_POOL_SIZE` sets the pool per worker. On SIGTERM, gunicorn stops
accepting connections, lets in-flight requests finish (`--graceful-timeout 30`,
compose `stop_grace_period: 40s`), and then the app's lifespan closes the pools.

## Authentication & roles

Every route except `/health/*` needs `Authorization: Bearer <key>`.

- **Key format:** keys look like `cg_<8 hex>_<43 chars>` (about 256 bits).
- **Shown once:** a key is displayed only when it is created. The database stores
  `sha256(key)` alone.
- **Lookup:** by the public prefix, then a constant-time hash compare.
- **One answer for every bad key:** unknown, malformed and revoked keys all get the
  same `401`, so callers learn nothing by probing.

```bash
python -m app.cli api-key create --name demo --role reviewer
python -m app.cli api-key revoke cg_3fa2b1c9          # key or prefix
```

| role     | can |
|----------|-----|
| viewer   | read everything |
| reviewer | viewer + upload, correct, resolve/reopen issues, approve/reject, reprocess, reconcile |
| admin    | reviewer + bulk reprocess, delete, stats |

Audit fields (`uploaded_by`, `corrected_by`, `resolved_by`, `reviewed_by`,
`triggered_by`, `created_by`) always come from the authenticated key's name, never from
the request body.

The browser never holds a key: the Next.js server (Step 8) proxies requests. CORS is
still configured, as an explicit allowlist (`CORS_ORIGINS`). The app refuses to start
with `"*"`.

## Conventions

- **Money** is a decimal **string** (`"1234.5600"`), never a float. Field values in
  results are strings too: amounts exact, dates ISO `YYYY-MM-DD`.
- **Timestamps** are UTC ISO 8601 with `Z`. **Enums** are strings.
- **Request ids:** `X-Request-ID` is accepted (`[A-Za-z0-9._-]{1,128}`) or generated,
  returned on every response, written to every log line, and passed to Celery tasks.
  One id traces a request through the API and the workers.
- **Input bounds:** UUIDs are validated; strings and numbers are bounded; `limit` ≤ 100;
  `q` ≤ 500 characters.

### Errors: RFC 9457 `application/problem+json`

```json
{
  "type": "https://careguide.dev/problems/version_mismatch",
  "title": "Precondition Failed",
  "status": 412,
  "detail": "The extraction changed (lock_version is 4); reload and retry.",
  "instance": "/api/v1/extractions/3fa2…",
  "code": "version_mismatch",
  "request_id": "req-demo-0001",
  "errors": [{"loc": ["body", "fields", "total"], "msg": "not an amount: 'twelve'", "type": "invalid_total"}]
}
```

- **`code`** is stable; branch on it, not on `detail`.
- **`errors[]`** carries field-level problems (422).
- **Unexpected errors** return a generic `500 internal_error` with the request id.
  Stack traces stay in the logs.

### Pagination

- **Keyset, not offsets:** `GET /documents?limit=25`
  → `{"items": [...], "next_cursor": "eyJ…"}`. Pass `?cursor=` for the next page;
  `null` means the last page.
- **Cursor contents:** the cursor is opaque. It encodes the last row's sort key,
  (sort value, `created_at`, `id`).
- **Stable pages:** rows inserted while you page never cause duplicates or skips.

### ETags (cheap polling)

`GET /documents/{id}`, `GET /documents/{id}/results`, `GET /extractions/{id}` and the
`PATCH` response carry a strong `ETag`. Send it back as `If-None-Match` to get `304`
with no body. The document ETag ignores `progress.elapsed_ms`, which changes on every
read; compute elapsed time from `progress.started_at` instead.

### Idempotent uploads

Send `Idempotency-Key: <8-128 chars of [A-Za-z0-9._:-]>` with `POST /documents`.

- **Replays:** within 24 h (`IDEMPOTENCY_TTL_S`), a replay returns the original
  response and ingests nothing.
- **Concurrency:** a concurrent replay gets `409 idempotency_in_progress`.
- **Scope:** keys are per API key.
- **Failures:** a request that failed with an error releases its key, so the client
  can retry with the same one.

### Optimistic locking for corrections

`PATCH /extractions/{id}` requires `If-Match: <lock_version>` (from the extraction).
A mismatch returns `412 version_mismatch`; a missing header returns `428 if_match_required`.

### Rate limits (per API key, Redis sliding window, shared by all API processes)

| class  | routes | default |
|--------|--------|---------|
| upload | `POST /documents` | `UPLOAD_RPM=30`/min |
| write  | PATCH, POST, DELETE | `WRITE_RPM=120`/min |
| read   | GET | `READ_RPM=600`/min |

Over the limit: `429 rate_limited` with `Retry-After` (seconds). A rejected request
doesn't use up a slot. Order of checks: 401 → 403 → 429, so an unauthenticated flood
never spends a real key's budget.

### Request size limits

- **In the app:** `MAX_REQUEST_SIZE_MB` (default 200) applies to the whole request; the
  Content-Length is checked first, then bytes are counted while streaming, and an
  oversized request gets `413 request_too_large`. `MAX_FILE_SIZE_MB` applies per file
  (`413 file_too_large`, checked before anything is ingested).
- **At the reverse proxy:** enforce the same limit there too:

```
# Caddy
request_body { max_size 200MB }
# nginx
client_max_body_size 200m;
```

### Security headers

Every response sets:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (`/docs` gets the
  minimum the Swagger UI needs)

Set `DOCS_ENABLED=false` in production to disable `/docs` and `/openapi.json`, or keep
them behind auth at the proxy.

**Logging:** each request writes one access line: request_id, principal name, method,
route, status and latency. File contents, extracted text and keys are never logged.

## Endpoints

| method | path | role | notes |
|---|---|---|---|
| POST | `/documents` | reviewer (upload) | multipart `files`, 1..`MAX_FILES_PER_UPLOAD` → 202 `[UploadOutcome]`; `Idempotency-Key` |
| GET | `/documents` | viewer | filters below, `sort=created_at\|invoice_date\|total`, `order=asc\|desc`, cursor |
| GET | `/documents/{id}` | viewer | metadata, status, error, `latest_job`, `progress` (ETag) |
| GET | `/documents/{id}/events` | viewer | processing history, cursor-paginated |
| GET | `/documents/{id}/file` | viewer | original bytes; detected `Content-Type`; sanitized `Content-Disposition: inline` |
| GET | `/documents/{id}/units/{index}/image?dpi=110` | viewer | PNG of a page/frame; cached; dpi in `PREVIEW_DPI_MIN..MAX`; 404 for sheets/CSV |
| GET | `/documents/{id}/results` | viewer | current extractions, per-field evidence, line items, active issues, reconciliations (ETag) |
| DELETE | `/documents/{id}` | admin | soft delete → 204 |
| POST | `/documents/{id}/reprocess` | reviewer | `{from_stage: parse\|extract\|validate, force}` → 202 job; 409 `job_active` |
| GET | `/extractions/{id}` | viewer | one extraction (ETag) |
| GET | `/extractions/{id}/versions` | viewer | every version of this invoice, newest first |
| GET | `/extractions/{id}/corrections` | viewer | audit trail |
| PATCH | `/extractions/{id}` | reviewer | corrections; `If-Match` required |
| POST | `/extractions/{id}/approve` | reviewer | `{note?}`; 409 `unresolved_errors` |
| POST | `/extractions/{id}/reject` | reviewer | `{note}` required |
| POST | `/issues/{id}/resolve` | reviewer | `{note}`; the issue stops blocking approval |
| POST | `/issues/{id}/reopen` | reviewer | 409 `not_resolved` if it isn't |
| POST | `/jobs/reprocess-failed` | admin | `{error_codes?, failed_since?, limit ≤ 500, force}` → `{queued, skipped, items}` |
| GET | `/jobs/{id}` | viewer | |
| POST | `/reconciliations` | reviewer | `{left_document_ids, right_document_ids}` → 202 `{run_id}` |
| GET | `/reconciliations/{run_id}` | viewer | status + report (matched, mismatched, only_in_left/right, ambiguous) |
| GET | `/search?q=&mode=keyword` | viewer | + the list filters; `semantic`/`hybrid` → 501 until Step 7 |
| GET | `/stats` | admin | counts by status/review status, open issues by rule, p50/p95 per file type, last 24 h |
| GET | `/health/live` | none | the process is up |
| GET | `/health/ready` | none | DB, Redis, storage; 503 with per-component `{ok, detail}` |

**Filters** (on `GET /documents` and `GET /search`):

| filter | how it matches |
|---|---|
| `status`, `review_status` | exact |
| `file_type` | `pdf, jpeg, png, tiff, xlsx, xls, csv` |
| `supplier` | trigram similarity on the normalized supplier name |
| `customer` | case-insensitive substring |
| `invoice_number` | normalized: `inv-2024/001` = `INV 2024 001` |
| `invoice_date_from`, `invoice_date_to`, `total_min`, `total_max`, `currency` | ranges and exact values |
| `has_open_issues`, `rule_code` | an open issue on a current extraction |
| `uploaded_from`, `uploaded_to` | upload time |

Invoice-field filters match **any** current invoice in the document. Sorting by
`invoice_date` or `total` uses the document's first invoice, and missing values sort last.

### Upload outcomes

```json
[{"filename": "a.pdf", "document_id": "…", "status": "queued", "duplicate_of_document_id": null, "error": null},
 {"filename": "x.pdf", "document_id": null, "status": "rejected",
  "error": {"code": "TYPE_MISMATCH", "detail": "The file content does not match its extension."}}]
```

- **Partial failure:** one bad file never fails the batch.
- **Rejections at intake** (type mismatch, unsupported, empty, zip bomb, macros) store
  nothing.
- **Damage found later:** problems found only while parsing (for example a truncated PDF
  body) show up on the document as `status: failed` with an error code.
- **Duplicates:** the same bytes are stored again and linked through
  `duplicate_of_document_id`.

### Corrections

```json
PATCH /api/v1/extractions/{id}
If-Match: 3
{"fields": {"total": "120.00", "invoice_date": "2026-03-01", "customer_name": null},
 "line_items": [{"op": "update", "id": "…", "amount": "60.00"},
                {"op": "add", "description": "Shipping", "amount": "10.00"},
                {"op": "delete", "id": "…"}],
 "reason": "OCR misread the total"}
```

- **Whitelist:** only `invoice_number, invoice_date, supplier_name, customer_name,
  subtotal, tax, total, currency` and line-item `description, quantity, unit_price,
  amount, tax_rate`. Anything else returns 422. Omit a field to leave it alone; send
  `null` to clear it.
- **Parsing:** values go through the pipeline's own normalizers. Ambiguous input
  (`"1,234"`, `"03/04/2026"`) is refused with a field error rather than guessed; use
  `1234.00` and ISO dates.
- **One transaction:** the field changes, the `corrections` audit rows (old value, new
  value, actor, reason), `field_meta` (`source=manual`, `confidence=1.0`), the recomputed
  match keys, the `lock_version` bump and re-validation (trigger `correction`) all commit
  together. The response is the updated extraction with fresh issues and a new ETag.
- **Current version only:** correcting an old version returns `409 not_current`.

## Reliability

- **Commit, then enqueue:** every job row is committed before its Celery message is
  sent. If the broker is down, the API still answers 202 and the job waits as a row.
- **One active job per document:** at most one queued or running job per document,
  enforced by the partial unique index `uq_processing_jobs_document_id_active`. A
  concurrent reprocess therefore gets 409 even under a race.
- **Sweeper (Celery beat, every `SWEEPER_INTERVAL_S`, idempotent):**
  - It re-enqueues jobs that have been queued longer than `STALE_QUEUED_MINUTES`.
    Quota-deferred jobs are skipped because they already have a scheduled task.
  - It fails jobs that have been running longer than `JOB_MAX_RUNTIME_MINUTES`, with
    `WORKER_TIMEOUT`. They then show up in bulk reprocessing.
  - Rows are locked `FOR UPDATE SKIP LOCKED`.
  - Run exactly one beat process.
- **Duplicate messages:** tasks skip jobs that already finished, so a redelivered or
  re-enqueued message never runs a job twice.

## Known limitations / follow-ups

- **Keyword search** covers the invoice number, supplier and customer only. Step 7 adds
  full-text and semantic search behind the same `SearchService`.
- **Search pagination:** search returns the top `limit` hits and has no cursor.
- **Previews** are cached forever per (document, unit, dpi), with no eviction yet.
- **Soft delete** doesn't cancel a job that is already running for the document.
- **Sweeper race:** a worker that comes back after the sweeper timed its job out can
  still write its results.
