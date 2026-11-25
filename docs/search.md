# Search

Keyword, semantic and hybrid search over processed invoices: `GET /api/v1/search`.

```
upload -> parse -> extract -> validate -> index (chunks, no network)  => keyword-searchable now
                                                    \-> embed task (quota permitting) => semantic too
```

| Module | Role |
|---|---|
| `app/search/chunking.py` | units -> chunks (pure) |
| `app/search/indexer.py` | writes a document's chunks, reusing unchanged embeddings |
| `app/search/embedding.py` | quota share + draining the pending queue |
| `app/search/keyword.py` | full text + field matches |
| `app/search/semantic.py` | query embedding (cached) + per-space k-NN |
| `app/search/fusion.py` | Reciprocal Rank Fusion (pure) |
| `app/search/snippets.py` | structured snippet segments (pure) |
| `app/search/service.py` | `HybridSearchService` (the `SearchService` the API uses) |
| `app/search/reembed.py` | fallback space -> primary space |

## Chunking

Chunking happens **per document unit** (a page, a frame, or a sheet). The target is
`CHUNK_TOKENS` (400), with `CHUNK_OVERLAP_TOKENS` (60) of trailing lines repeated at the
start of the next chunk, so a passage cut at a boundary is still found whole.

- **Token estimate:** `max(ceil(chars / 4), ceil(words × 1.3))`. That is about 4 characters
  per BPE token for prose, and the word term catches number- and code-heavy invoice text.
  It errs high, needs no tokenizer dependency, and every size check estimates the
  *assembled* chunk, so the cap holds after joining.
- **Line boundaries:** text is packed line by line. A line is split only when it alone
  exceeds the budget: at word boundaries, or at characters for a single giant "word".
- **Sheets and CSV:** chunks are packed by rows. Every chunk starts with `Sheet: <name>` and
  the header row, so each chunk is self-describing. The header may take at most half the
  budget.
- **Summary chunks:** each current extraction gets one, built from the normalized fields:
  *"Invoice INV-2026-001 from Acme Ltd to Beta Corp, dated 2026-03-01 (March 2026), total
  1,234.56 USD, subtotal 1,000.00, tax 234.56. Items: Consulting (1,000.00)."* The month
  name is what makes "acme invoices in march" work by keyword as well as by meaning.
  Summaries are regenerated when an extraction is corrected (in the same transaction as the
  correction) or re-extracted.
- **Invariants** (Hypothesis-tested): every input line appears in at least one chunk, and
  no chunk exceeds the cap.

## Embedding: off the critical path

1. **The index stage** (pipeline, after validate) writes the chunks with `embedding = NULL`.
   The document is `completed` or `needs_review` at that point and is already
   keyword-searchable. An indexing failure is recorded as an event and never fails the
   document.
2. **After the final commit**, the pipeline enqueues `search.embed_pending(document_id)` on
   the `llm` queue.
3. **The embed task** works through pending chunks (the partial index
   `ix_chunks_pending_embedding`) in batches of `EMBED_BATCH_SIZE`, one request per batch,
   and commits each batch. That makes it resumable and idempotent: a vector is stored only
   if the chunk's `content_sha256` is unchanged.
4. **A beat sweep** (`EMBED_SWEEP_INTERVAL_S`, default 15 min) picks up whatever is still
   pending.

**Change detection.** Re-chunking deletes and rebuilds a document's chunks, but a new chunk
whose `content_sha256` matches an already-embedded old chunk inherits its vector. Only
genuinely new text is sent for embedding.

### Quota budgeting

The free tier is **one** account-wide budget, of about 20 requests per minute plus a daily
cap, shared with extraction.

- **The share:** embeddings, both document chunks and search queries, may use at most
  `EMBED_QUOTA_SHARE` (0.3) of the day's limit. `EmbedBudget` counts each request in Redis
  *before* sending it.
- **When the share is spent, or the provider says the quota is exhausted:** the chunks stay
  pending and keyword search keeps working. The sweep after the UTC reset resumes. Nothing
  fails, and no task loops.
- **Why no per-document deferred task:** quota exhaustion would otherwise park thousands of
  ETA tasks in the broker. One periodic sweep is enough.
- **Why no `processing_jobs` row:** a queued embed job would hold the one-active-job index
  and make reprocessing return 409 for hours.

### Embedding spaces and re-embedding

A **space** is one model's vector space (`EMBEDDING_SPACES`: `nemotron-3.5-8b` primary,
`nemotron-3-1b` and `nemotron-3.5-1b` fallbacks). Vectors from different spaces are never
compared, even at equal dimension.

- **Fallback storage:** when the primary model is down, the gateway falls back to another
  space and the chunk is stored tagged with that space. It is searchable, but only by
  queries embedded in the same space.
- **Re-embedding** moves fallback-space chunks to the primary space when the primary's
  circuit is closed and the share allows. It is idempotent and resumable: each batch commits
  and only rows still in the source space are selected.
  - CLI: `python -m app.cli reembed --from-space nemotron-3-1b [--limit N]`
  - Beat: `search.reembed` every `REEMBED_INTERVAL_S`.

## Keyword search

- **Full text over `chunks.tsv`** with `websearch_to_tsquery('simple', q)`: `"quoted
  phrases"`, `or`, and `-exclusion`.
  - **Ranking:** `ts_rank_cd` (cover density: matched terms close together rank higher).
  - **Exclusion works on documents:** a document is dropped if *any* of its chunks matches
    an excluded term. Otherwise its summary chunk would bring back what the user excluded.
- **Field matches on current extractions:**
  - an exact `invoice_number_norm` match (`inv-2026/001` = `INV2026001`)
  - trigram similarity on `supplier_name_norm` and on `lower(customer_name)` (both GIN
    trigram indexes)
- **Invoice-number boost:** when the query looks like an invoice number (one code-like term
  with a digit), an exact number match is boosted to the top with score 1.0, so it beats
  documents that merely mention the code.

### Invoice codes and the 'simple' parser

The parser reads `INV-2026-001` as `inv`, `-2026`, `-001`: the hyphen is taken as a minus
sign. Unfixed, the full code matched only as a phrase, and `2026-001` or `001` never
matched. The fix, pinned on both sides by `tests/search/test_keyword.py`:

- **The index side:** the generated column is
  `to_tsvector('simple', regexp_replace(content, '([[:alnum:]])[-/](?=[[:alnum:]])', '\1 ', 'g'))`,
  so `-` and `/` between letters or digits become spaces.
- **The query side:** `prepare_websearch()` applies the same rule and wraps the result in a
  phrase, so `INV-2026-001` becomes `"INV 2026 001"` (all parts, in order). A leading `-`
  stays an exclusion.
- **The result:** the full code, `2026-001`, `001` and `2026-03-01` all match. The compact
  `INV2026001` is matched by the invoice-number field match.
- **No stemming by design:** `invoice` does not match `invoices` in full text (names and
  codes would be mangled by a stemmer). Semantic search covers that gap.

## Semantic search

1. **Which spaces?** Find the spaces that hold any embedded chunks (normally one).
   - **The probe:** each space's own HNSW index is probed (`ORDER BY embedding <=> const LIMIT
     1`), all in one round trip, with seqscan disabled for the probe only. That is about 1 ms
     even for an empty space.
   - **Why not an equality filter:** `WHERE embedding_space = …` with no index on that column
     would scan the table for every empty space, on every query.
   - **Filters:** they are applied in the vector search itself.
2. **Embed the query once per space** (`input_type = query`). Query vectors are cached in
   Redis per (normalized query, space) for `QUERY_EMBED_CACHE_TTL_S`. The key holds a sha256
   of the query, never its text. A cache hit spends no request.
3. **Search each space separately.** Each search runs
   `ORDER BY embedding <=> :q LIMIT k` with `embedding_space = '<space>'` rendered as a
   **literal**. A bind parameter can't be matched to a partial index's predicate at plan
   time, so the literal is what makes the planner use that space's partial HNSW index.
   `tests/search/test_semantic_hybrid.py::test_explain_uses_the_partial_hnsw_index` runs
   `EXPLAIN` on the real filtered query to check this.
4. **Score:** cosine distance d ∈ [0, 2] becomes similarity `1 − d/2` ∈ [0, 1].

### Filtered vector search (pgvector 0.8.7 here)

An HNSW scan returns its `ef_search` nearest candidates, and SQL filters apply *afterwards*.
A selective filter (say, one supplier) can therefore silently return fewer than k rows, or
none. Two settings, both transaction-local per query:

- `hnsw.ef_search = SEARCH_EF` (100): a bigger candidate pool.
- `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8): the scan keeps walking the graph
  until enough rows pass the filters, bounded by `hnsw.max_scan_tuples`. Results may come
  slightly out of order, so they are re-sorted by distance.

On pgvector < 0.8 the second setting doesn't exist. Only `ef_search` helps there, and
filtered queries can come back short.

## Hybrid ranking: Reciprocal Rank Fusion

The fused lists are:

- **keyword:** full text over chunks, and field matches
- **semantic:** one list per space

Mode `keyword` fuses the keyword lists, `semantic` the semantic lists, `hybrid` all of them.

```
score(r) = Σ over lists L of 1 / (RRF_K + rank_L(r))      RRF_K = 60, rank from 1
```

**Why RRF.** It uses **ranks only**. A `ts_rank_cd` of 0.07 and a cosine similarity of 0.83
are on unrelated scales, and their distributions shift per query. Min-max or z-score
normalization is fragile (one outlier rescales everything), while ranks are comparable by
construction. `k = 60` (Cormack et al., 2009) damps the head of each list: being in *both*
lists matters more than being first versus second in one.

**Aggregation.** Chunk hits are reduced to **result keys** (document, extraction) before
fusion.

- **One rank per key:** each list contributes a key's best rank once, so an invoice with
  twenty matching chunks isn't counted twenty times.
- **Snippet:** the best chunk is kept for it.
- **`match_type`:** `keyword`, `semantic` or `both`.

**Scores in the response** are RRF normalized by its ideal value: a result ranked first in
every list is 1.0, and an exact invoice-number match is 1.0.

**Pagination:** offset/limit over the fused ranking, capped at `MAX_SEARCH_RESULTS` (200).
Each list is `CANDIDATES_PER_LIST` (200) deep, so every page is reachable.

## Degradation (never a 500)

If the query can't be embedded, `semantic` and `hybrid` answer with **keyword** results,
`degraded: true`, and a `degraded_reason`. The causes:

- the embedding share is spent
- the provider quota is exhausted
- an outage, or an open circuit breaker
- the semantic side isn't configured
- nothing matching is embedded yet (for example, a fresh upload)

## Snippets: segments, never HTML

Results carry `snippet_segments: [{text, highlight}]`, and clients render them **as text**.
Document content is untrusted (it came from a PDF or a spreadsheet), and segments make
markup injection impossible by construction: there is nothing to escape and nothing to
forget to escape. A chunk containing `<script>…</script>` comes back verbatim as inert text
(tested).

- **Keyword hits:** `ts_headline` runs **only for the returned page**, because it re-parses
  every chunk it is given.
  - **Sentinels:** it marks matches with private-use characters, which Python turns into
    segments.
  - **Masked `<`:** `ts_headline` drops anything shaped like a tag, so `<` is masked before
    the call and restored afterwards.
  - **Fallback:** if `ts_headline` marked nothing (for example, a part of a code), the query
    words are highlighted in Python.
- **Semantic hits:** the 30-word window of the chunk that shares the most query words.
- **Field hits:** for example `Supplier: <Acme> Supplies Ltd`.

## Rate limit and logging

- **Rate limit:** search has its own per-key limit, `SEARCH_RPM` (60/min), stricter than
  other reads, because a semantic query can spend embedding quota.
- **Logging:** each query logs its mode, per-phase latency (`spaces`, `embed`, `keyword`,
  `vector`, `fuse`, `hydrate`), result count, spaces and `degraded`. The query text is
  **never** logged at INFO.

## Benchmark

`python scripts/bench_search.py [--chunks 100000]` seeds synthetic chunks into a throwaway
database. Results are in [benchmarks/search.md](benchmarks/search.md): p50/p95 latency per
mode, and HNSW recall@10 against exact search at `ef_search` 40, 100 and 200.

**Index builds in Docker.** A parallel HNSW build allocates shared memory beyond Docker's
default 64 MB `/dev/shm`. The benchmark builds serially. For production index builds, set
`shm_size: 1gb` on the `db` service, or `max_parallel_maintenance_workers = 0`.

## Configuration

| setting | default | meaning |
|---|---|---|
| `CHUNK_TOKENS` / `CHUNK_OVERLAP_TOKENS` | 400 / 60 | chunk size and overlap |
| `EMBED_QUOTA_SHARE` | 0.3 | max share of the daily free budget for embeddings (docs + queries) |
| `EMBED_PENDING_LIMIT` | 256 | chunks per embed task run |
| `EMBED_SWEEP_INTERVAL_S` / `REEMBED_INTERVAL_S` | 900 / 3600 | beat schedules |
| `QUERY_EMBED_CACHE_TTL_S` | 604800 | query-vector cache |
| `SEARCH_EF` | 100 | `hnsw.ef_search` per query |
| `CANDIDATES_PER_LIST` | 200 | depth of each list before fusion |
| `RRF_K` | 60 | RRF damping |
| `MAX_SEARCH_RESULTS` | 200 | offset + limit cap |
| `INVOICE_NUMBER_BOOST` | 1.0 | added to an exact invoice-number match |
| `SEARCH_MIN_SIMILARITY` | 0.3 | trigram threshold for supplier/customer matches |
| `SEARCH_RPM` | 60 | search requests per minute per API key |

## Limitations

- **Search depth:** search ranks at most 200 results. It is for finding, not exporting
  (use `GET /documents` with filters for exhaustive lists).
- **No stemming in full text:** "invoices" doesn't match "invoice" by keyword, only
  semantically once embedded.
- **Fallback-space chunks** are searchable only by queries embedded in that space, until
  re-embedded.
- **Shared-unit chunks:** content chunks of a unit shared by two invoices have no
  `extraction_id`, so invoice-field filters exclude them (summary chunks still match).
- **Benchmark vectors:** the benchmark uses random vectors, the hardest case for HNSW, so
  recall on real embeddings is higher.
- **Cache invalidation:** query embeddings are cached per space. A model change within a
  space needs a cache flush (`search:qvec:*`).
