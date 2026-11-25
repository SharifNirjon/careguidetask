"""Live extraction smoke test: 3 synthetic invoices, at most 6 LLM requests.

    docker compose run --rm worker python /app/scripts/smoke_extraction.py

1. a digital PDF, 2. a scanned (OCR'd) PDF, 3. an invoice whose printed total
is deliberately wrong (subtotal + tax != total). Each goes through the real
pipeline (ingest -> parse -> segment -> extract) and is persisted as a
versioned extraction. The wrong total must come back EXACTLY as printed.
Synthetic data only: free endpoints may log traffic.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "backend"), str(REPO)]

from sqlalchemy import select  # noqa: E402

from app.logging_setup import configure_logging  # noqa: E402
from app.models import Document, Extraction  # noqa: E402
from app.pipeline import orchestrator  # noqa: E402
from app.pipeline.intake import ingest_file  # noqa: E402
from tests.fixtures import factory  # noqa: E402

MAX_REQUESTS = 6
WRONG_TOTAL = "999.99"
CHECKED = ("invoice_number", "invoice_date", "supplier_name", "subtotal", "tax", "total", "currency")


@dataclass(frozen=True)
class Case:
    name: str
    data: bytes
    expected: dict[str, str]


def lines(number: str, total: str = "270.00") -> list[str]:
    return [
        line.replace("Total Amount Due 270.00 EUR", f"Total Amount Due {total} EUR")
        for line in factory.invoice_lines(number)
    ]


def cases() -> list[Case]:
    base = {
        "invoice_date": "2024-03-05",
        "supplier_name": "ACME Supplies Ltd",
        "subtotal": "225.0000",
        "tax": "45.0000",
        "total": "270.0000",
        "currency": "EUR",
    }
    return [
        Case("digital.pdf", factory.digital_pdf([lines("SYN-1001")]), {**base, "invoice_number": "SYN-1001"}),
        Case("scanned.pdf", factory.scanned_pdf(lines("SYN-2002")), {**base, "invoice_number": "SYN-2002"}),
        Case(
            "wrong_total.pdf",
            factory.digital_pdf([lines("SYN-3003", WRONG_TOTAL)]),
            {**base, "invoice_number": "SYN-3003", "total": "999.9900"},
        ),
    ]


def run(ctx: orchestrator.PipelineContext, case: Case) -> Extraction | None:
    with ctx.session() as session:
        doc = ingest_file(
            io.BytesIO(case.data),
            case.name,
            "smoke",
            session,
            storage=ctx.storage,
            enqueue=lambda *_: None,
            settings=ctx.settings,
        )
        doc_id, job_id = doc.id, doc.jobs[0].id
    status = orchestrator.run_document(ctx, doc_id, job_id)
    with ctx.session() as session:
        document = session.get_one(Document, doc_id)
        print(
            f"\n{case.name}: document {status.value}"
            + (f" ({document.error_code.value})" if document.error_code else "")
        )
        return session.scalars(
            select(Extraction).where(Extraction.document_id == doc_id, Extraction.is_current.is_(True))
        ).first()


def report(case: Case, extraction: Extraction | None) -> bool:
    if extraction is None:
        print("  no extraction persisted")
        return False
    print(
        f"  extraction v{extraction.version}  model={extraction.model_name}  "
        f"prompt={extraction.prompt_version}  overall_conf={extraction.overall_confidence}"
    )
    ok = True
    for name in CHECKED:
        got = getattr(extraction, name)
        got_s = "-" if got is None else str(got)
        meta = extraction.field_meta.get(name, {})
        match = got_s == case.expected[name]
        ok &= match or name not in ("invoice_number", "total")
        print(
            f"  {'OK ' if match else 'XX '}{name:15} expected={case.expected[name]:20} got={got_s:20} "
            f"grounded={meta.get('grounded')}  evidence={str(meta.get('evidence_text'))[:40]!r}"
        )
    return ok


def main() -> int:
    configure_logging()
    ctx = orchestrator.default_context()
    from redis import Redis

    from app.config import get_llm_settings
    from app.llm.factory import build_gateways

    settings = get_llm_settings()
    quota = build_gateways(settings, Redis.from_url(settings.redis_url)).quota
    before = quota.resync() or quota.status()
    print(f"quota before: {before.used}/{before.limit} used, {before.remaining} remaining")
    if before.remaining < MAX_REQUESTS:
        print(f"need {MAX_REQUESTS} requests of headroom; aborting")
        return 2

    results = [(case, run(ctx, case)) for case in cases()]
    print("\n=== fields: expected vs extracted ===")
    all_ok = True
    for case, extraction in results:
        print(f"\n{case.name}")
        all_ok &= report(case, extraction)
    wrong = results[2][1]
    kept = wrong is not None and wrong.total == Decimal(WRONG_TOTAL)
    print(f"\nwrong total kept exactly as printed ({WRONG_TOTAL}): {'YES' if kept else 'NO'}")

    after = quota.resync() or quota.status()
    print(
        f"quota after: {after.used}/{after.limit} used, {after.remaining} remaining "
        f"(this run: {after.used - before.used} requests, budget {MAX_REQUESTS})"
    )
    return 0 if all_ok and kept and after.used - before.used <= MAX_REQUESTS else 1


if __name__ == "__main__":
    sys.exit(main())
