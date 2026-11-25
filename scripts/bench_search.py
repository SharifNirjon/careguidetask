"""Search benchmark: latency per mode and HNSW recall, written to docs/benchmarks/search.md.

    python scripts/bench_search.py                 # 100k chunks (~10 min: serial HNSW build)
    python scripts/bench_search.py --chunks 20000  # quicker

Runs in its own throwaway database (<test db>_bench), never the dev or test data.

Synthetic data that behaves like the real thing:
  - vectors: a 48-dim Gaussian latent projected to 1024 dims, plus noise, then
    L2-normalized. Real embeddings have a low intrinsic dimension; i.i.d. random
    1024-dim vectors do not (all pairwise distances concentrate, so "the 10 nearest"
    is a set of near-ties that no ANN index can recover; recall would measure the
    data, not the index).
  - text: 60 words per chunk drawn Zipf-style from a 3,000-word vocabulary, so a
    query term is selective, like real searches.
  - query vectors: stored vectors plus noise (near-duplicates, not exact copies).
The query-embedding call is excluded from latency (a real provider call is
network-bound and spends quota); it is reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import platform
import random
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "docs" / "benchmarks" / "search.md"
SPACE = "nemotron-3.5-8b"
DIM = 1024
LATENT_DIM = 48
VECTOR_NOISE = 0.15  # ambient noise added to every projected vector
QUERY_NOISE = 0.35  # query = normalize(data vector + QUERY_NOISE * gaussian / sqrt(DIM))
CHUNKS_PER_DOCUMENT = 20
WORDS_PER_CHUNK = 60
VOCAB_SIZE = 3000
QUERY_RANKS = (50, 1500)  # query words come from the middle of the Zipf curve (selective, not rare)
COPY_BLOCK = 2000
EF_VALUES = (40, 100, 200)
K = 10
BASE_WORDS = [
    "invoice",
    "total",
    "subtotal",
    "tax",
    "vat",
    "net",
    "gross",
    "amount",
    "due",
    "payment",
    "terms",
    "supplier",
    "customer",
    "consulting",
    "services",
    "delivery",
    "shipping",
    "freight",
    "widgets",
    "chairs",
    "desks",
    "laptops",
    "monitors",
    "licenses",
    "subscription",
    "credit",
    "refund",
    "discount",
    "order",
    "purchase",
    "reference",
    "account",
    "transfer",
    "clinic",
    "hospital",
    "pharmacy",
    "maintenance",
    "repair",
    "catering",
]


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct * len(ordered)) - 1)]


@dataclass
class Timing:
    name: str
    samples: list[float]

    def row(self) -> str:
        return (
            f"| {self.name} | {statistics.median(self.samples):.1f} | {percentile(self.samples, 0.95):.1f} "
            f"| {len(self.samples)} |"
        )


def vocabulary() -> list[str]:
    return BASE_WORDS + [f"term{i:04d}" for i in range(VOCAB_SIZE - len(BASE_WORDS))]


def seed_rows(engine: Any, chunks: int) -> None:
    """documents, units and extractions (cheap, in SQL)."""
    from sqlalchemy import text

    documents = max(1, chunks // CHUNKS_PER_DOCUMENT)
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO documents (original_filename, storage_key, mime_type, file_ext, size_bytes, sha256,
                                   status)
            SELECT 'bench-' || g || '.pdf', 'bench/' || g, 'application/pdf', 'pdf', 1000,
                   encode(sha256(('bench' || g)::bytea), 'hex'), 'completed'
            FROM generate_series(1, :n) g"""),
            {"n": documents},
        )
        conn.execute(
            text("""
            INSERT INTO document_units (document_id, unit_type, unit_index, text, extraction_method)
            SELECT id, 'pdf_page', 0, 'bench', 'digital' FROM documents""")
        )
        conn.execute(
            text("""
            INSERT INTO extractions (document_id, invoice_index, unit_start, unit_end, invoice_number,
                                     invoice_number_norm, supplier_name, supplier_name_norm, currency, total,
                                     lock_version)
            SELECT d.id, 0, 0, 0, 'BENCH-' || n, 'BENCH' || n,
                   (ARRAY['Acme Ltd','Globex Corp','Initech'])[1 + n % 3],
                   (ARRAY['acme','globex','initech'])[1 + n % 3],
                   (ARRAY['EUR','USD','GBP'])[1 + n % 3], 100, 1
            FROM (SELECT id, row_number() OVER () AS n FROM documents) d""")
        )


def seed_chunks(engine: Any, seed: int) -> None:
    """Vectors and texts generated with numpy, loaded with a binary COPY."""
    import numpy as np
    from pgvector.psycopg import register_vector
    from sqlalchemy import text

    from app.models.search import hnsw_index_name

    gen = np.random.default_rng(seed)
    projection = gen.standard_normal((LATENT_DIM, DIM)).astype(np.float32)
    words = np.array(vocabulary())
    zipf = 1.0 / np.arange(1, len(words) + 1)
    zipf /= zipf.sum()
    with engine.connect() as sa_conn:
        units = sa_conn.execute(
            text(
                "SELECT u.id, u.document_id, e.id FROM document_units u "
                "JOIN extractions e ON e.document_id = u.document_id ORDER BY u.document_id"
            )
        ).all()
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {hnsw_index_name(SPACE)}")  # build once, after loading
            rows = [(u, i) for u in units for i in range(CHUNKS_PER_DOCUMENT)]
            columns = (
                "document_id, unit_id, extraction_id, chunk_index, chunk_kind, content, content_sha256, "
                "token_count, embedding, embedding_model, embedding_space, embedded_at"
            )
            now = datetime.now(UTC)
            with cur.copy(f"COPY chunks ({columns}) FROM STDIN WITH (FORMAT BINARY)") as copy:
                # content_sha256 is CHAR(64): binary bpchar is byte-identical to text on the wire
                copy.set_types(
                    [
                        "uuid",
                        "uuid",
                        "uuid",
                        "int4",
                        "text",
                        "text",
                        "text",
                        "int4",
                        "vector",
                        "text",
                        "text",
                        "timestamptz",
                    ]
                )
                for start in range(0, len(rows), COPY_BLOCK):
                    block = rows[start : start + COPY_BLOCK]
                    latent = gen.standard_normal((len(block), LATENT_DIM)).astype(np.float32)
                    vectors = latent @ projection / math.sqrt(LATENT_DIM)
                    vectors += VECTOR_NOISE * gen.standard_normal(vectors.shape).astype(np.float32)
                    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
                    texts = gen.choice(words, size=(len(block), WORDS_PER_CHUNK), p=zipf)
                    for ((unit_id, document_id, extraction_id), index), vector, tokens in zip(
                        block, vectors, texts, strict=True
                    ):
                        content = " ".join(tokens)
                        copy.write_row(
                            (
                                document_id,
                                unit_id,
                                extraction_id,
                                index,
                                "content",
                                content,
                                hashlib.sha256(content.encode()).hexdigest(),
                                80,
                                vector,
                                "bench",
                                SPACE,
                                now,
                            )
                        )
        conn.commit()
    finally:
        raw.close()


def build_index(engine: Any) -> None:
    from sqlalchemy import text

    from app.models.search import hnsw_index_name

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL maintenance_work_mem = '700MB'"))
        # Parallel builds use /dev/shm, 64 MB by default in Docker (see docs/search.md): build serially.
        conn.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))
        conn.execute(
            text(
                f"CREATE INDEX {hnsw_index_name(SPACE)} ON chunks USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = 16, ef_construction = 64) WHERE embedding_space = '{SPACE}'"
            )
        )
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM ANALYZE chunks"))


def query_vectors(engine: Any, count: int, rng: random.Random) -> list[list[float]]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT embedding::text FROM chunks TABLESAMPLE SYSTEM (1) LIMIT :n"), {"n": count}
        ).all()
    vectors = []
    for (raw,) in rows:
        base = [float(x) for x in raw.strip("[]").split(",")]
        noisy = [v + QUERY_NOISE * rng.gauss(0, 1) / math.sqrt(len(base)) for v in base]
        norm = math.sqrt(sum(v * v for v in noisy))
        vectors.append([v / norm for v in noisy])
    return vectors


def recall(engine: Any, vectors: list[list[float]]) -> tuple[list[str], dict[int, float]]:
    from sqlalchemy import text

    sql = text(
        f"SELECT id FROM chunks WHERE embedding_space = '{SPACE}' "
        "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
    )
    rows, recalls = [], {}
    exact: list[set[Any]] = []
    with engine.connect() as conn:
        for v in vectors:
            with conn.begin():
                conn.execute(text("SET LOCAL enable_indexscan = off"))  # exact: sequential scan
                exact.append(set(conn.scalars(sql, {"v": str(v), "k": K})))
        for ef in EF_VALUES:
            found, times = [], []
            for v, truth in zip(vectors, exact, strict=True):
                with conn.begin():
                    conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
                    started = time.perf_counter()
                    ids = set(conn.scalars(sql, {"v": str(v), "k": K}))
                    times.append((time.perf_counter() - started) * 1000)
                found.append(len(ids & truth) / K)
            recalls[ef] = statistics.mean(found)
            rows.append(
                f"| {ef} | {recalls[ef]:.3f} | {statistics.median(times):.1f} | "
                f"{percentile(times, 0.95):.1f} |"
            )
    return rows, recalls


class PrecomputedEmbedder:
    """Stands in for QueryEmbedder: vectors are known up front (no provider call, no quota)."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, query: str, space: str) -> list[float]:
        return self._vectors[query]


def service_latency(
    session_factory: Callable[[], Any], vectors: list[list[float]], rng: random.Random
) -> list[Timing]:
    from app.config import SearchSettings
    from app.search.filters import DocumentFilters
    from app.search.service import HybridSearchService
    from app.search.types import SearchMode, SearchQuery

    pool = vocabulary()[QUERY_RANKS[0] : QUERY_RANKS[1]]
    queries = [f"{a} {b}" for a, b in (rng.sample(pool, 2) for _ in vectors)]
    queries = [f"{q} #{i}" for i, q in enumerate(queries)]  # unique keys for the precomputed vectors
    embedder = PrecomputedEmbedder(dict(zip(queries, vectors, strict=True)))
    service = HybridSearchService(settings=SearchSettings(), min_similarity=0.3, query_embedder=embedder)  # type: ignore[arg-type]
    cases = [
        ("keyword", SearchMode.KEYWORD, DocumentFilters()),
        ("semantic", SearchMode.SEMANTIC, DocumentFilters()),
        ("semantic + filter (currency, ~1/3 of rows)", SearchMode.SEMANTIC, DocumentFilters(currency="EUR")),
        ("hybrid", SearchMode.HYBRID, DocumentFilters()),
    ]
    results = []
    for name, mode, filters in cases:
        totals = []
        for q in queries:
            with session_factory() as session:
                started = time.perf_counter()
                result = service.search(session, SearchQuery(q, mode, filters, 25))
                elapsed = (time.perf_counter() - started) * 1000
            totals.append(elapsed - result.timings_ms.get("embed", 0.0))
            assert not result.degraded, result.degraded_reason
        results.append(Timing(name, totals))
    return results


def versions(engine: Any) -> tuple[str, str]:
    from sqlalchemy import text

    with engine.connect() as conn:
        pg = conn.scalar(text("SHOW server_version")) or "?"
        vec = conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")) or "?"
    return str(pg).split(" ")[0], str(vec)


def write_report(
    chunks: int,
    timings: dict[str, float],
    latency: list[Timing],
    recall_rows: list[str],
    recalls: dict[int, float],
    pg: str,
    vec: str,
    queries: int,
) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    low, mid, high = (recalls[ef] for ef in EF_VALUES)
    lines = [
        "# Search benchmark",
        "",
        f"Generated by `scripts/bench_search.py` on {datetime.now(UTC):%Y-%m-%d} "
        f"({platform.system()} {platform.machine()} laptop, PostgreSQL {pg} + pgvector {vec} in Docker, "
        "shared with the dev stack).",
        "",
        f"- **Data:** {chunks:,} chunks in one embedding space, {chunks // CHUNKS_PER_DOCUMENT:,} documents. "
        f"The vectors are 1024-dim projections of a {LATENT_DIM}-dim latent plus noise (real embeddings also "
        f"have a low intrinsic dimension). Each text is {WORDS_PER_CHUNK} Zipf-distributed words from a "
        f"{VOCAB_SIZE:,}-word vocabulary.",
        f"- **Load:** {timings['load_s']:.0f} s. Serial HNSW build (m=16, ef_construction=64): "
        f"{timings['hnsw_build_s']:.0f} s.",
        f"- **Queries:** {queries} per row. Latency is end-to-end service time for a 25-hit page "
        "(SQL, fusion and snippets), excluding the query-embedding call.",
        "",
        "## Latency (ms)",
        "",
        "| mode | p50 | p95 | n |",
        "|---|---:|---:|---:|",
        *[t.row() for t in latency],
        "",
        "The query-embedding call is not in these numbers. It is a network call to the provider (hundreds of "
        "ms on the free tier) and is cached in Redis per (normalized query, space), so a repeated query "
        "skips it.",
        "",
        f"## HNSW recall@{K} vs exact search",
        "",
        "Exact = the same query with index scans disabled (sequential scan, the true top 10).",
        "",
        "| hnsw.ef_search | recall@10 | p50 ms | p95 ms |",
        "|---:|---:|---:|---:|",
        *recall_rows,
        "",
        f"Recall changes by {mid - low:+.3f} from ef_search 40 to 100 and by {high - mid:+.3f} "
        "from 100 to 200. "
        "SEARCH_EF=100 is the default.",
        "",
    ]
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--keep", action="store_true", help="keep the bench database afterwards")
    args = parser.parse_args()

    from sqlalchemy.orm import sessionmaker

    from tests.support.db import database_url_for_tests, drop_database, make_migrated_engine

    rng = random.Random(args.seed)
    url = database_url_for_tests()
    url = url.set(database=f"{url.database}_bench")
    print(f"creating {url.database} ...", flush=True)
    engine = make_migrated_engine(url)
    try:
        print(f"seeding {args.chunks:,} chunks ...", flush=True)
        started = time.perf_counter()
        seed_rows(engine, args.chunks)
        seed_chunks(engine, args.seed)
        timings = {"load_s": time.perf_counter() - started}
        print(f"  load {timings['load_s']:.0f}s; building HNSW ...", flush=True)
        started = time.perf_counter()
        build_index(engine)
        timings["hnsw_build_s"] = time.perf_counter() - started
        print(f"  hnsw build {timings['hnsw_build_s']:.0f}s", flush=True)
        vectors = query_vectors(engine, args.queries, rng)
        print("recall ...", flush=True)
        recall_rows, recalls = recall(engine, vectors)
        print("  " + ", ".join(f"ef={ef}: {r:.3f}" for ef, r in recalls.items()), flush=True)
        print("service latency ...", flush=True)
        latency = service_latency(sessionmaker(bind=engine), vectors, rng)
        for t in latency:
            print(
                f"  {t.name}: p50 {statistics.median(t.samples):.1f} ms, "
                f"p95 {percentile(t.samples, 0.95):.1f} ms"
            )
        pg, vec = versions(engine)
        write_report(args.chunks, timings, latency, recall_rows, recalls, pg, vec, len(vectors))
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
    finally:
        engine.dispose()
        if not args.keep:
            drop_database(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
