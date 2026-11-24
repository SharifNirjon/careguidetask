"""Parse benchmark: real numbers for the evaluation report.

    docker compose run --rm worker python /app/scripts/bench_parse.py
    (options: --digital-pages 1000 --scanned-pages 50)

Generates a digital PDF and a scanned PDF, parses each in a FRESH child process
exactly like the workers do (batches of PAGE_BATCH_SIZE, file reopened per
batch), and reports pages/sec per method, p50/p95 per-page time, total time
and the child's peak RSS (from os.wait4, so generation cost is not included).
Results are written to docs/benchmarks/parse.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "backend"), str(REPO)]

KB = 1024
OUTPUT = REPO / "docs" / "benchmarks" / "parse.md"


# ------------------------------------------------------------------ fixtures


def generate(digital_pages: int, scanned_pages: int, out_dir: Path) -> dict[str, Path]:
    import pymupdf

    from tests.fixtures import factory

    digital = out_dir / "digital.pdf"
    pages = [
        factory.invoice_lines(f"INV-{i // 3:05d}", i % 3 + 1, 3, totals=i % 3 == 2)
        for i in range(digital_pages)
    ]
    digital.write_bytes(factory.digital_pdf(pages))

    scanned = out_dir / "scanned.pdf"
    doc = pymupdf.open()
    for i in range(scanned_pages):
        image = factory.degrade(
            factory.render_page_image(factory.invoice_lines(f"INV-S{i:04d}")), rotate_deg=(i % 5) - 2
        )
        factory._image_page(doc, image)
    doc.save(scanned)
    return {"digital": digital, "scanned": scanned}


# --------------------------------------------------------------------- child


def parse_child(path: Path, result_file: Path) -> None:
    """Runs in a fresh process: parse like a worker, write per-page timings."""
    from app.config import get_pipeline_settings
    from app.pipeline import pdf
    from app.pipeline.ocr.tesseract import TesseractEngine

    settings = get_pipeline_settings()
    engine = TesseractEngine(settings.ocr_langs, settings.ocr_page_timeout_s)
    started = time.perf_counter()
    info = pdf.inspect_pdf(path, settings)
    per_page: list[tuple[str, int]] = []
    failed = 0
    for start in range(0, info.page_count, settings.page_batch_size):
        units = pdf.parse_pdf_pages(
            path, start, start + settings.page_batch_size, engine=engine, settings=settings
        )
        per_page += [(u.extraction_method.value, u.duration_ms) for u in units]
        failed += sum(u.is_failed for u in units)
        del units  # a worker persists and drops each batch
    result_file.write_text(
        json.dumps(
            {
                "pages": info.page_count,
                "failed": failed,
                "total_s": time.perf_counter() - started,
                "per_page": per_page,
                "batch_size": settings.page_batch_size,
                "ocr_dpi": settings.ocr_dpi,
            }
        )
    )


def run_child(path: Path) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        result_file = Path(tmp.name)
    proc = subprocess.Popen([sys.executable, __file__, "--child", str(path), str(result_file)])
    _, status, usage = os.wait4(proc.pid, 0)
    if status != 0:
        raise RuntimeError(f"benchmark child failed with status {status}")
    data = json.loads(result_file.read_text())
    result_file.unlink()
    data["peak_rss_mb"] = usage.ru_maxrss / KB  # Linux reports KiB
    return data


# -------------------------------------------------------------------- report


def pct(values: list[int], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def summarize(name: str, data: dict) -> list[str]:
    by_method: dict[str, list[int]] = defaultdict(list)
    for method, ms in data["per_page"]:
        by_method[method].append(ms)
    rows = []
    for method, values in sorted(by_method.items()):
        busy_s = sum(values) / 1000
        rows.append(
            f"| {name} | {data['pages']} | {method} | {len(values)} | "
            f"{len(values) / busy_s:.2f} | {pct(values, 50):.0f} | {pct(values, 95):.0f} | "
            f"{data['total_s']:.1f} | {data['peak_rss_mb']:.0f} | {data['failed']} |"
        )
    return rows


def tesseract_version() -> str:
    out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=False)
    return (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else "unknown"


def write_report(results: dict[str, dict]) -> str:
    import pymupdf

    lines = [
        "# Parse benchmark",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC} by `scripts/bench_parse.py`.",
        "",
        "Each file is parsed in a fresh process the way a worker does it (batches of "
        f"{results['digital']['batch_size']} pages, file reopened per batch, OCR at "
        f"{results['digital']['ocr_dpi']} DPI). Pages/sec is per method over the time spent "
        "on those pages; peak RSS is the child process's `ru_maxrss` (generation excluded).",
        "",
        "| file | pages | method | n | pages/sec | p50 ms | p95 ms | total s | peak RSS MB | failed |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, data in results.items():
        lines += summarize(name, data)
    lines += [
        "",
        "Environment:",
        "",
        f"- CPUs visible: {os.cpu_count()}, single worker process, `OMP_THREAD_LIMIT="
        f"{os.environ.get('OMP_THREAD_LIMIT', 'unset')}`",
        f"- {platform.python_implementation()} {platform.python_version()} on {platform.platform()}",
        f"- PyMuPDF {pymupdf.__version__}, {tesseract_version()}",
        "",
        "Throughput scales roughly linearly with worker processes on the `ocr` queue "
        "(one Tesseract process per worker, no shared state).",
        "",
        "Peak RSS is the Python worker process only; Tesseract runs as a short-lived "
        "subprocess per page and is not included.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--digital-pages", type=int, default=1000)
    parser.add_argument("--scanned-pages", type=int, default=50)
    parser.add_argument("--child", nargs=2, metavar=("PDF", "RESULT_JSON"))
    args = parser.parse_args()
    if args.child:
        parse_child(Path(args.child[0]), Path(args.child[1]))
        return

    with tempfile.TemporaryDirectory() as tmp:
        files = generate(args.digital_pages, args.scanned_pages, Path(tmp))
        results = {name: run_child(path) for name, path in files.items()}
    report = write_report(results)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report, encoding="utf-8")
    sys.stdout.write(report)


if __name__ == "__main__":
    main()
