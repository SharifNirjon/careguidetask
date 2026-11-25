"""Synthetic input files for the frontend Playwright e2e (frontend/e2e/fixtures).

    python scripts/make_e2e_fixtures.py

All content is generated (no real invoices): free LLM endpoints may log traffic.
The invoice's printed total deliberately does not add up (225.00 + 45.00 is
270.00, but it says 300.00), so validation always raises TOTAL_MISMATCH and the
e2e can correct it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from tests.fixtures import factory  # noqa: E402

OUT = ROOT / "frontend" / "e2e" / "fixtures"
INVOICE_NUMBER = "E2E-2026-0042"

MISMATCHED_INVOICE = [
    "INVOICE",
    f"Invoice Number: {INVOICE_NUMBER}",
    "Date: 2026-03-05",
    "Bill To: Northwind Clinic, 12 Harbour Road",
    "Supplier: ACME Supplies Ltd",
    "",
    "Description            Qty    Unit Price    Amount",
    "Nitrile gloves (box)    10         12.50    125.00",
    "Surgical masks           5         20.00    100.00",
    "",
    "Subtotal 225.00",
    "VAT 20% 45.00",
    "Total Amount Due 300.00 EUR",
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "invoice-mismatch.pdf": factory.digital_pdf([MISMATCHED_INVOICE]),
        "scanned-receipt.png": factory.blurry_png(),
        "ledger.xlsx": factory.invoice_xlsx(),
        "corrupt.pdf": b"%PDF-1.7\n" + b"\x00garbage" * 64,
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print(f"wrote {OUT.relative_to(ROOT) / name} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
