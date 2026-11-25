# Architecture

An invoice document-intelligence system: upload PDFs, scans, images, Excel and CSV
files; the pipeline parses them (OCR where needed), splits multi-invoice files,
extracts invoice fields with an LLM (or with rules for spreadsheets), validates them,
indexes them for search, and sends anything doubtful to a human reviewer.

## Components

```mermaid
flowchart LR
    subgraph Browser
        UI["Review UI<br/>(React, TanStack Query)"]
    end

    subgraph FE["frontend container (Next.js 16, Node)"]
        BFF["Backend-for-frontend<br/>login (iron-session cookie)<br/>/api/proxy allowlist<br/>adds the API key"]
    end

    subgraph BE["api container (FastAPI, gunicorn + uvicorn)"]
        API["REST API /api/v1<br/>auth (API keys, roles)<br/>rate limits, ETags, problem+json"]
    end

    subgraph W["worker container (Celery)"]
        PIPE["Pipeline tasks<br/>parse / OCR / segment<br/>extract / validate"]
        EMB["Embedding tasks<br/>search.embed_pending"]
    end

    BEAT["beat container<br/>(sweeper, embed sweep)"]
    PG[("PostgreSQL 16<br/>pgvector + pg_trgm")]
    RD[("Redis<br/>broker, rate limits,<br/>LLM quota and cache")]
    FS[("storage volume<br/>original files")]
    OR["OpenRouter<br/>(free LLM + embeddings)"]
    TES["Tesseract OCR<br/>(in the worker image)"]

    UI -- "HTTPS, session cookie<br/>(no API key in the browser)" --> BFF
    BFF -- "Bearer API key" --> API
    API --> PG
    API --> RD
    API --> FS
    API -- "enqueue jobs" --> RD
    RD --> PIPE
    RD --> EMB
    BEAT --> RD
    PIPE --> PG
    PIPE --> FS
    PIPE --> TES
    PIPE -- "chat completions" --> OR
    EMB -- "embeddings" --> OR
    EMB --> PG
```

| Container | Image | Responsibility |
|---|---|---|
| `frontend` | `frontend/Dockerfile` (Node 24, standalone Next.js, non-root) | The review UI and its server: login, sessions, a proxy that only forwards allowlisted API paths and adds the API key server-side. |
| `api` | `backend/Dockerfile` | The public REST API ([api.md](api.md), [openapi.json](openapi.json)). Uploads are streamed to storage, registered, and queued. |
| `worker` | `backend/Dockerfile` (with Tesseract) | Celery worker on the `ocr`, `default` and `llm` queues: the processing pipeline and embeddings. |
| `beat` | `backend/Dockerfile` | Exactly one scheduler: stale-job sweeper (`WORKER_TIMEOUT`), embedding sweep, re-embedding. |
| `db` | `pgvector/pgvector:pg16` | All state: documents, units, extractions (versioned), issues, events, chunks with vectors. |
| `redis` | `redis` | Celery broker, API rate limits, LLM daily quota and rate limiter, LLM response cache. |

## Processing flow

```mermaid
sequenceDiagram
    autonumber
    participant U as Reviewer (UI)
    participant A as API
    participant Q as Redis (queue)
    participant W as Worker
    participant L as LLM (OpenRouter)
    participant D as PostgreSQL

    U->>A: POST /documents (files, Idempotency-Key)
    A->>A: stream to storage, sha256, detect real type, size and bomb checks
    A->>D: document + job + "ingest" event (or reject with an error code)
    A->>Q: enqueue parse
    A-->>U: 202 per-file outcomes
    Q->>W: pipeline.parse_batch (25 pages per task, in parallel)
    W->>W: PDF text layer or OCR per page / images / sheets / CSV
    W->>D: document_units (text, tables, method, OCR confidence)
    W->>W: segment: invoice boundaries in multi-invoice files
    W->>L: extract each segment (strict JSON schema, temperature 0)<br/>spreadsheets: rules, no LLM call
    W->>W: ground every value in the document text, normalize, score confidence
    W->>D: extraction version (fields, line items, field_meta)
    W->>D: validate: arithmetic, dates, currency, duplicates, cross-document checks
    W->>D: status: completed (auto-approved) or needs_review
    W->>D: chunks (keyword-searchable at once)
    W->>L: embeddings later, within the daily quota share
    U->>A: GET /documents/{id}/results (ETag polling)
    U->>A: PATCH corrections (If-Match), resolve issues, approve / reject
```

Every stage writes a `processing_events` row (stage, status, duration), so the
timeline in the UI and the processing-time statistics come from the same data. Stages
are idempotent: a retried or redelivered task creates no duplicate rows.

## Key design decisions

| Decision | Why |
|---|---|
| **Backend-for-frontend** instead of calling the API from the browser | The API key never reaches the browser; sessions are httpOnly cookies; the proxy allowlists paths and checks the origin on mutations. |
| **Celery stages with page batches** | OCR is slow (seconds per page); a 1,000-page file is split into parallel 25-page tasks, and a failed batch retries cheaply. |
| **Text layer first, OCR only where needed** | Digital PDFs parse at about 1,500 pages/s; OCR runs at a fraction of a page per second ([benchmarks/parse.md](benchmarks/parse.md)). |
| **Rules for spreadsheets, LLM for documents** | Clean tables need no model and cost no quota; the LLM handles free-form layouts. |
| **Grounding check on every LLM value** | The model must cite evidence found in the document; anything else is rejected, so values are never invented. |
| **Validation never changes values** | It reports issues with the computed expectation; a human applies a suggestion. |
| **Versioned extractions, append-only corrections and events** | Full audit history; optimistic locking (ETag / If-Match) protects concurrent reviewers. |
| **Quota-aware LLM gateway** | Shared rate limiter and daily quota in Redis, response cache, model fallbacks, circuit breaker; quota exhaustion defers work instead of failing it. |
| **PostgreSQL for search** (tsvector + pgvector HNSW + RRF) | One database, transactional consistency, filters in SQL; the HNSW index finds 0.968 of the exact top 10 at the default `ef_search` = 100 ([benchmarks/search.md](benchmarks/search.md)). |

## Database schema

The full schema (tables, constraints, indexes, the entity-relationship diagram) is in
[database.md](database.md).

![Schema](images/schema.png)

## Further reading

| Area | Document |
|---|---|
| Ingestion and parsing pipeline (PDF, OCR, images, Excel, CSV) | [pipeline.md](pipeline.md) |
| AI extraction and the LLM gateway | [extraction.md](extraction.md) |
| Validation rules and the review policy | [validation.md](validation.md) |
| Search | [search.md](search.md) |
| REST API | [api.md](api.md) |
| Review UI | [frontend.md](frontend.md) |
| Tests and results | [testing/matrix.md](testing/matrix.md), [test-results/summary.md](test-results/summary.md) |
| Evaluation | [EVALUATION.md](EVALUATION.md) |
