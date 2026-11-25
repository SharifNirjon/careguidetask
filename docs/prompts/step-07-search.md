<!-- Prompt given to Claude Code for this step, verbatim (sent 2026-11-25 13:45 UTC). -->

# Role
You are a senior backend engineer building search for a production document-intelligence
system: keyword filtering, semantic search and hybrid ranking over processed invoices.
Results must be relevant, fast and safe to render, and search must keep working when the
free embedding provider is rate-limited. If anything here is wrong or risky, say so
before coding.

# Context
- Steps 2–6 are done. Read the models, backend/app/llm (EmbeddingGateway: batching,
  embedding spaces, 1024-dim truncation, quota handling), the orchestrator, and the
  /search route (semantic/hybrid currently return 501). Follow CLAUDE.md.
- chunks table: document_id, unit_id, extraction_id, chunk_index, content, token_count,
  embedding vector(1024), embedding_model, embedding_space, tsv (generated, 'simple'),
  created_at.
- Embedding spaces: "nemotron-8b" (primary) and "nemotron-1b" (fallback). Vectors from
  different spaces are NEVER compared.
- Every embedding request, including each search query, spends free-tier quota
  (~20/min plus a daily cap).
- The orchestrator has a no-op `embed` stage stub.

# Scope
IN: chunking, embed stage, re-embedding, keyword full-text search, semantic search,
hybrid ranking, filters, snippets, SearchService wired into /search, migration, final
database documentation (the schema deliverable), tests, benchmark, docs.
OUT: frontend (Step 8).

# 1. Chunking (backend/app/search/chunking.py) — pure
- Chunk per document unit. Target CHUNK_TOKENS (default 400) with CHUNK_OVERLAP_TOKENS
  (default 60). Estimate tokens with a cheap, documented heuristic (no heavy tokenizer
  dependency).
- Split on line boundaries. Never split mid-line unless a single line exceeds the limit.
- Sheets/CSV: chunk by rows and repeat the header row at the top of every chunk, so each
  chunk is self-describing.
- Add one extraction_summary chunk per current extraction, built from the normalized
  fields, e.g. "Invoice INV-2026-001 from Acme Ltd to Beta Corp, dated 2026-03-01,
  total 1,234.56 USD. Items: …". This is what makes "acme invoices in march" work.
- Store chunk_kind (content | extraction_summary) and content_sha256 (change detection).

# 2. Embed stage (replace the stub)
- Embedding is NOT on the critical path. It runs after validate, and the document's
  status (completed / needs_review) never waits for it. Keyword search works as soon as
  chunks exist.
- Insert chunks first (embedding NULL), then embed in batches of EMBED_BATCH_SIZE.
  Skip chunks whose content_sha256, model and space are unchanged.
- Quota budget: embeddings may use at most EMBED_QUOTA_SHARE (default 0.3) of the daily
  free budget, so extraction is never starved. When that share is used up, or
  QuotaExhaustedError is raised: leave the chunks pending and defer the job. Never fail
  the document.
- Store embedding_model, embedding_space and embedded_at on each chunk.
- When an extraction is corrected (Step 6) or re-extracted, regenerate its
  extraction_summary chunk.

# 3. Re-embedding
- `python -m app.cli reembed --from-space nemotron-1b [--limit N]`, plus a low-priority
  Celery beat task that does the same automatically.
- It moves fallback-space chunks to the primary space when the primary model is healthy
  (circuit closed) and quota is available.
- Idempotent, resumable, and it respects the quota share.

# 4. Keyword search
- Full text over chunks.tsv with websearch_to_tsquery('simple', q), which supports
  "quoted phrases", OR and -exclusion. Rank with ts_rank_cd.
- Field matching on extractions:
  - exact invoice_number_norm match, boosted to the top when q looks like an invoice
    number
  - trigram similarity on supplier_name_norm and on the customer name
- Check how the 'simple' parser tokenizes codes like "INV-2026-001", and handle it
  (with a test).

# 5. Semantic search
- Find which spaces have embedded chunks matching the filters (normally just one).
- Embed the query once per space with input_type=query. Cache query embeddings in Redis
  (key: normalized query + space; TTL QUERY_EMBED_CACHE_TTL).
- Per space: ORDER BY embedding <=> :query_vec LIMIT k, with the predicate
  embedding_space = :space so the partial HNSW index is used.
- Filtered vector search: set hnsw.ef_search (SEARCH_EF, default 100) per query. On
  pgvector ≥ 0.8, enable iterative index scans so filters don't silently drop results.
  Check the installed pgvector version and document the behavior.
- Convert distance to a similarity score in [0, 1].

# 6. Hybrid ranking
- Reciprocal Rank Fusion (k = RRF_K, default 60) over the keyword list and each semantic
  list. RRF needs no score normalization; explain this in the docs.
- Aggregate chunk hits to extraction/document level: the best chunk rank wins, and the
  best snippet is kept. Label each result's match_type: keyword | semantic | both.
- Degradation: if the query embedding fails (quota, outage, open circuit), hybrid and
  semantic fall back to keyword. The response includes degraded: true and a reason.
  This never returns a 500.

# 7. Filters, pagination, response
- Same filters as GET /documents (status, review_status, file_type, supplier, customer,
  invoice_number, date range, total range, currency, has_open_issues, rule_code),
  applied in SQL together with keyword and vector search.
- Ranked results use offset pagination, capped at MAX_SEARCH_RESULTS (default 200).
- Result shape: {document_id, extraction_id, score, match_type, headline fields,
  unit_index, snippet_segments: [{text, highlight: bool}]}. This replaces Step 6's
  snippet string.
- Snippets are STRUCTURED segments, never HTML:
  - Run ts_headline only on the final page of results (it's expensive), using sentinel
    markers that Python converts into segments.
  - For semantic hits, take the best-matching window of the chunk.
  - This makes XSS impossible by construction.
- Give the search endpoint a stricter rate limit than other reads (SEARCH_RPM), because
  semantic queries spend quota.

# 8. SearchService
- Implement the Step 6 SearchService protocol and replace the 501s in /search. Update
  the API tests and the openapi.json snapshot.
- Log per query: mode, latency per phase (embed, keyword, vector, fuse), result count and
  degraded. Never log the query text at INFO.

# 9. Migration (one)
- chunks:
  - add chunk_kind TEXT NOT NULL and content_sha256 CHAR(64)
  - make embedding, embedding_model and embedding_space nullable (set when embedded)
  - add embedded_at TIMESTAMPTZ
- Replace the single HNSW index with one partial HNSW index per configured space
  (WHERE embedding_space = '<space>').
- Add an index on (document_id, chunk_index), a partial index WHERE embedding IS NULL
  (the pending work queue), and a trigram expression index on lower(customer_name).
- Justify anything else first.

# 10. Database documentation (assignment deliverable: database schema)
Update docs/database.md to the FINAL schema after all migrations (Steps 2–7):
- A Mermaid ER diagram of every table, plus a rendered PNG at docs/images/schema.png
  (mermaid-cli; if it can't run in the container, document the manual export).
- Each table's purpose, every index and why it exists, the extraction/issue versioning
  model, the audit trail, and the search model (chunks, spaces, partial indexes).
- A retention and scale plan (monthly partitioning of processing_events and chunks,
  archiving): documented, not implemented.

# 11. Tests (tests/search/)
- Chunker: line boundaries, overlap, an oversized line, sheet header repetition, summary
  chunk content. Hypothesis invariants: every input line appears in ≥ 1 chunk, and no
  chunk exceeds the cap.
- Embed stage:
  - batching works; unchanged chunks are skipped
  - the quota share is respected
  - deferral leaves chunks keyword-searchable
  - fallback space is tagged correctly
  - the summary chunk is regenerated after a correction
- Keyword: phrases, OR and exclusion; "INV-2026-001" is found by the full code (and by
  its parts where appropriate); an exact invoice number is boosted.
- Semantic, using a deterministic FakeEmbeddingGateway (hash-based bag-of-words vectors,
  so similar texts are close):
  - the relevant chunk ranks first
  - space isolation: a query in one space never scores chunks from the other
  - filters are applied
  - a query cache hit spends no request
- Hybrid: RRF math unit test; degraded mode when embedding fails.
- Snippets: content containing "<script>" comes back as plain-text segments.
- Integration: EXPLAIN shows the partial HNSW index is used; reembed moves chunks
  between spaces.

# 12. Benchmark (scripts/bench_search.py)
Seed N synthetic chunks (default 100k: random unit vectors in one space, plus generated
text). Report:
- p50/p95 latency for keyword, semantic and hybrid (the embedding call is excluded and
  reported separately)
- HNSW recall@10 vs exact search at ef_search 40 / 100 / 200
Write the results to docs/benchmarks/search.md.

# 13. Docs (docs/search.md)
Chunking strategy; embedding spaces and re-embedding; keyword vs semantic vs hybrid; RRF;
filtered vector search caveats; degradation behavior; quota budgeting; snippet safety;
benchmark results; limitations.

# Working style
1. Before coding: restate the plan in ≤10 bullets, list assumptions, flag disagreements.
2. Order: migration → chunking → embed stage → keyword → semantic → hybrid → service +
   API → reembed → benchmark → docs.
3. Finish with: files created/changed, commands, benchmark numbers, follow-ups.

# Acceptance criteria
- `pytest tests/search tests/api -q` passes; ruff, `ruff format --check` and mypy pass.
- A processed document is keyword-searchable before its embeddings finish.
- Semantic and hybrid return relevant results. With embeddings blocked, they degrade to
  keyword with degraded: true.
- EXPLAIN confirms the partial HNSW index is used; benchmark results are written.
- docs/database.md reflects the final schema, with a rendered ER diagram.
- `alembic upgrade head` / `alembic downgrade -1` work.
