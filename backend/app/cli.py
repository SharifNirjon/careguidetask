"""Command-line entry point for local runs.

    python -m app.cli ingest <path-or-folder> [--sync] [--uploaded-by NAME]

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

from app.logging_setup import configure_logging
from app.models import Document, DocumentUnit
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


def print_table(rows: Sequence[Row]) -> None:
    cells = [[str(getattr(r, c)) for c in COLUMNS] for r in rows]
    widths = [
        max(len(c), *(len(row[i]) for row in cells)) if cells else len(c) for i, c in enumerate(COLUMNS)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(COLUMNS, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="ingest a file or every file in a folder")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--sync", action="store_true", help="run the pipeline inline")
    ingest.add_argument("--uploaded-by", default="cli")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)  # keep the table readable; JSON logs go to stderr
    if not args.path.exists():
        parser.error(f"{args.path} does not exist")
    rows = ingest_paths(args.path, sync=args.sync, uploaded_by=args.uploaded_by)
    print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
