"""Command-line entry point for local runs.

    python -m app.cli ingest <path-or-folder> [--sync] [--uploaded-by NAME]
    python -m app.cli llm-check [--probe]
    python -m app.cli extract <document_id> [--force]
    python -m app.cli api-key create --name NAME --role viewer|reviewer|admin
    python -m app.cli api-key revoke <key-or-prefix>
    python -m app.cli reembed --from-space nemotron-3-1b [--limit N]

Without --sync the documents are enqueued to Celery; with --sync the whole
pipeline runs inline and a summary table is printed.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app import cli_llm
from app.config import EMBEDDING_SPACES
from app.logging_setup import configure_logging
from app.models import ApiRole, Document, DocumentUnit
from app.pipeline import orchestrator
from app.pipeline.errors import PermanentError
from app.pipeline.intake import ingest_file


@dataclass(slots=True)
class Row:
    file: str
    detected: str = "-"
    status: str = "-"
    error_code: str = "-"
    units: int = 0
    digital: int = 0
    ocr: int = 0
    avg_conf: str = "-"
    segments: int = 0
    seconds: float = 0.0


COLUMNS = (
    "file",
    "detected",
    "status",
    "error_code",
    "units",
    "digital",
    "ocr",
    "avg_conf",
    "segments",
    "seconds",
)


def _files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.is_file())
    return [target]


def _summarize(ctx: orchestrator.PipelineContext, document_id: uuid.UUID, row: Row) -> None:
    with ctx.session() as session:
        doc = session.get_one(Document, document_id)
        row.detected = doc.mime_type.split("/")[-1][:24]
        row.status = doc.status.value
        row.error_code = doc.error_code.value if doc.error_code else "-"
        row.segments = len(doc.segments or [])
        units = session.execute(
            select(DocumentUnit.extraction_method, DocumentUnit.ocr_confidence).where(
                DocumentUnit.document_id == document_id
            )
        ).all()
    methods = Counter(u.extraction_method.value for u in units)
    confidences = [float(u.ocr_confidence) for u in units if u.ocr_confidence is not None]
    row.units, row.digital, row.ocr = len(units), methods["digital"], methods["ocr"]
    row.avg_conf = f"{statistics.fmean(confidences):.2f}" if confidences else "-"


def _run_inline_later(document_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """--sync: nothing to enqueue; the caller runs the pipeline right after intake."""


def ingest_paths(target: Path, *, sync: bool, uploaded_by: str) -> list[Row]:
    ctx = orchestrator.default_context()
    enqueue: Callable[[uuid.UUID, uuid.UUID], None]
    if sync:
        enqueue = _run_inline_later
    else:
        from app.workers.tasks import enqueue_document as enqueue  # needs Redis

    rows: list[Row] = []
    for path in _files(target):
        row = Row(file=path.name)
        started = time.perf_counter()
        try:
            with ctx.session() as session, open(path, "rb") as fh:
                doc = ingest_file(
                    fh,
                    path.name,
                    uploaded_by,
                    session,
                    storage=ctx.storage,
                    enqueue=enqueue,
                    settings=ctx.settings,
                )
                document_id, job_id = doc.id, doc.jobs[0].id
            if sync:
                orchestrator.run_document(ctx, document_id, job_id)
            _summarize(ctx, document_id, row)
        except PermanentError as exc:  # rejected at intake: no document row
            row.status, row.error_code = "failed", exc.code.value
            row.detected = "rejected"
        row.seconds = round(time.perf_counter() - started, 2)
        rows.append(row)
    return rows


def print_rows(columns: Sequence[str], rows: list[list[str]]) -> None:
    """Plain aligned table on stdout (the CLI is the only place that prints)."""
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    print()


def print_table(rows: Sequence[Row]) -> None:
    print_rows(COLUMNS, [[str(getattr(r, c)) for c in COLUMNS] for r in rows])


def api_key_command(action: str, *, name: str | None, role: str | None, key: str | None) -> int:
    """The new key is printed ONCE (stdout only); only its hash is stored."""
    from app.api.services.auth import AuthService
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as session:
        service = AuthService(session)
        if action == "create":
            generated = service.create_key(name or "unnamed", ApiRole(role or ApiRole.VIEWER.value))
            print(f"prefix: {generated.prefix}")
            print(f"key:    {generated.key}")
            print("Store the key now: it cannot be shown again.")
            return 0
        if service.revoke_key(key or ""):
            print("revoked")
            return 0
    print("no active key with that prefix", file=sys.stderr)
    return 1


def reembed_command(from_space: str, limit: int) -> int:
    """Move fallback-space chunks to the primary space (same code as the beat task)."""
    from redis import Redis

    from app.config import get_llm_settings, get_search_settings
    from app.db.session import get_sessionmaker
    from app.search.runtime import build_runtime

    llm = get_llm_settings()
    runtime = build_runtime(llm, get_search_settings(), Redis.from_url(llm.redis_url))
    report = runtime.reembedder(get_sessionmaker(), llm.embed_batch_size).run(
        from_spaces=[from_space], limit=limit
    )
    print_rows(
        ["from_space", "to_space", "selected", "moved", "note"],
        [
            [
                from_space,
                runtime.primary_space,
                str(report.selected),
                str(report.moved),
                report.skipped_reason or "-",
            ]
        ],
    )
    return 0 if report.skipped_reason is None else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="ingest a file or every file in a folder")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--sync", action="store_true", help="run the pipeline inline")
    ingest.add_argument("--uploaded-by", default="cli")
    check = sub.add_parser("llm-check", help="show the free-tier quota and configured models")
    check.add_argument("--probe", action="store_true", help="send one tiny request per model (costs quota)")
    extract = sub.add_parser("extract", help="(re-)extract one document and show every field")
    extract.add_argument("document_id", type=uuid.UUID)
    extract.add_argument("--force", action="store_true", help="bypass the LLM response cache")
    keys = sub.add_parser("api-key", help="create or revoke API keys")
    key_sub = keys.add_subparsers(dest="action", required=True)
    create = key_sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=[r.value for r in ApiRole])
    revoke = key_sub.add_parser("revoke")
    revoke.add_argument("key", help="the key or its prefix (cg_xxxxxxxx)")
    re_embed = sub.add_parser("reembed", help="move chunks from a fallback embedding space to the primary")
    re_embed.add_argument("--from-space", required=True, choices=list(EMBEDDING_SPACES))
    re_embed.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)  # keep the tables readable; JSON logs go to stderr
    if args.command == "llm-check":
        return cli_llm.llm_check(probe=args.probe, print_rows=print_rows)
    if args.command == "reembed":
        return reembed_command(args.from_space, args.limit)
    if args.command == "api-key":
        return api_key_command(
            args.action,
            name=getattr(args, "name", None),
            role=getattr(args, "role", None),
            key=getattr(args, "key", None),
        )
    if args.command == "extract":
        return cli_llm.extract(args.document_id, force=args.force, print_rows=print_rows)
    if not args.path.exists():
        parser.error(f"{args.path} does not exist")
    print_table(ingest_paths(args.path, sync=args.sync, uploaded_by=args.uploaded_by))
    return 0


if __name__ == "__main__":
    sys.exit(main())
