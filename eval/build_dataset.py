"""Generate eval/dataset/ (files + manifest.jsonl) with tools/synth, fixed seed.

    python -m eval.build_dataset            # rewrites eval/dataset/files and the manifest

Composition is scaled to the free-tier quota (50 LLM requests/day without
credits): categories that cost LLM requests are smaller than the Step 10 brief's
targets, the free ones (spreadsheets, CSV, invalid files) keep their size. See
docs/EVALUATION.md, "Setup", for the justification and the numbers.
"""

from __future__ import annotations

import random
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from eval.manifest import DATASET_DIR, ExpectedInvoice, ManifestEntry, sha256_of, write_manifest
from tools.synth import InvoiceFactory
from tools.synth import render as R
from tools.synth.model import LAYOUTS, Invoice

SEED = 20261125
FUTURE = date(2027, 12, 31)
DEV_SHARE = 0.3
FILES = DATASET_DIR / "files"
SCORED_RULES = (
    "TOTAL_MISMATCH",
    "LINE_SUM_MISMATCH",
    "MISSING_FIELD",
    "FUTURE_DATE",
    "UNKNOWN_CURRENCY",
    "DUPLICATE_INVOICE",
    "INVOICE_NUMBER_CONFLICT",
    "CROSS_DOC_MISMATCH",
)
REQUIRED = {  # mirrors ValidationSettings.required_fields (error + warning severities)
    "invoice": ("invoice_number", "invoice_date", "supplier_name", "total", "currency", "customer_name",
                "subtotal", "tax"),
    "credit_note": ("invoice_number", "invoice_date", "supplier_name", "total", "currency", "customer_name",
                    "subtotal", "tax"),
    "receipt": ("invoice_date", "supplier_name", "total", "currency", "invoice_number", "customer_name"),
}  # fmt: skip


@dataclass
class Item:
    file: str
    data: bytes
    category: str
    fmt: str
    invoices: list[Invoice] = field(default_factory=list)
    truths: list[dict[str, Any]] | None = None  # override (spreadsheets: no line items)
    layout: str | None = None
    degradation: str = "none"
    error_code: str | None = None
    extra_rules: tuple[str, ...] = ()
    group: str | None = None
    llm_requests: int = 0
    notes: str = ""


def implied_rules(truth: dict[str, Any], document_type: str) -> set[str]:
    """Rule codes a correct system must raise for this ground truth."""
    rules: set[str] = set()
    for name in REQUIRED[document_type]:
        if name != "currency" and truth.get(name) is None:
            rules.add("MISSING_FIELD")
    if truth.get("currency") is None:
        # The generator always prints a symbol: a bare "$" is UNKNOWN_CURRENCY,
        # not MISSING_FIELD ("one problem, one issue").
        rules.add("UNKNOWN_CURRENCY")
    return rules


def sheet_truth(inv: Invoice) -> dict[str, Any]:
    truth = inv.truth()
    truth["line_items"] = []
    truth["currency"] = inv.currency
    return truth


def sheet_rules(truth: dict[str, Any]) -> set[str]:
    return {"MISSING_FIELD"} if truth["customer_name"] is None or truth["tax"] is None else set()


def build(seed: int = SEED) -> list[ManifestEntry]:
    f = InvoiceFactory(seed)
    rng = random.Random(seed + 1)
    items: list[Item] = []

    # -- digital PDFs: 2 per layout (16)
    for layout in LAYOUTS:
        for j in range(2):
            kwargs: dict[str, Any] = {"layout": layout}
            notes = ""
            if layout == "table_heavy" and j == 1:
                kwargs["n_items"] = 28
                notes = "long line-item table (2 pages)"
            if layout == "classic" and j == 1:
                kwargs["currency_style"] = "symbol_only"
                kwargs["missing_customer"] = True
                notes = "bare currency symbol, no Bill To"
            if layout == "modern" and j == 1:
                kwargs["date_style"] = "slash_ambiguous"
                kwargs["invoice_date"] = date(2026, 3, 4)
                notes = "ambiguous date 04/03/2026 (DD/MM vs MM/DD)"
            if layout == "minimal" and j == 0:
                kwargs["no_tax"] = True
                notes = "no tax line"
            inv = f.invoice(**kwargs)
            items.append(
                Item(f"ev-digital-{layout}-{j + 1}.pdf", R.invoice_pdf(inv), "digital_pdf", "pdf", [inv],
                     layout=layout, llm_requests=1, notes=notes)
            )  # fmt: skip

    # -- scanned PDFs: 3 per degradation level (9)
    for level in ("mild", "medium", "heavy"):
        for j in range(3):
            layout = ("classic", "modern", "european")[j]
            inv = f.invoice(layout=layout)
            items.append(
                Item(f"ev-scan-{level}-{j + 1}.pdf", R.scanned_pdf(inv, level, rng), "scanned_pdf", "pdf", [inv],
                     layout=layout, degradation=level, llm_requests=1)
            )  # fmt: skip

    # -- images (6 files, 7 invoices)
    for name, fmt, level in (("png-mild", "PNG", "mild"), ("png-medium", "PNG", "medium"),
                             ("jpg-mild", "JPEG", "mild"), ("jpg-heavy", "JPEG", "heavy")):  # fmt: skip
        inv = f.invoice(layout=rng.choice(("classic", "receipt", "modern")))
        ext = "jpg" if fmt == "JPEG" else "png"
        items.append(
            Item(f"ev-image-{name}.{ext}", R.image_file(inv, fmt, level, rng), "image", ext, [inv],  # type: ignore[arg-type]
                 layout=inv.layout, degradation=level, llm_requests=1)
        )  # fmt: skip
    inv = f.invoice(layout="classic")
    items.append(
        Item("ev-image-warped.jpg", R.warped_jpeg(inv, rng), "image", "jpg", [inv], layout="classic",
             degradation="mild", llm_requests=1, notes="perspective warp")
    )  # fmt: skip
    pair = [f.invoice(layout="classic"), f.invoice(layout="modern")]
    items.append(
        Item("ev-image-multiframe.tiff", R.multiframe_tiff(pair, "mild", rng), "image", "tiff", pair,
             layout="classic", degradation="mild", llm_requests=2, notes="2 frames, one invoice per frame")
    )  # fmt: skip

    # -- spreadsheets (7 XLSX + 3 XLS), several invoices per sheet
    def regs(n: int) -> list[Invoice]:
        return [f.invoice(layout="classic", currency_style="code") for _ in range(n)]

    xlsx_specs: list[tuple[str, dict[str, Any], list[tuple[str, list[Invoice]]], str]] = [
        ("register-basic", {}, [("Invoices", regs(3))], "one invoice per row"),
        ("register-offset", {"title_rows": 3}, [("Invoices", regs(4))], "header on row 4, merged title"),
        ("register-merged", {"title_rows": 1, "merge_currency": True}, [("Q3", regs(3))], "currency merged"),
        ("register-multisheet", {"notes_sheet": True}, [("July", regs(2)), ("August", regs(3))],
         "two register sheets + a notes sheet"),
        ("register-numeric", {"numeric_cells": True}, [("Invoices", regs(4))], "numeric and date cells"),
    ]  # fmt: skip
    for name, kw, sheets, notes in xlsx_specs:
        invs = [inv for _, group in sheets for inv in group]
        items.append(
            Item(f"ev-sheet-{name}.xlsx", R.register_xlsx(sheets, **kw), "spreadsheet", "xlsx", invs,
                 truths=[sheet_truth(i) for i in invs], notes=notes)
        )  # fmt: skip
    for n in range(2):
        invs = [f.invoice(layout="classic", n_items=rng.randint(2, 4)) for _ in range(2)]
        truths = [i.truth() for i in invs]
        for t in truths:
            t["currency"] = next(i.currency for i in invs if i.truth()["invoice_number"] == t["invoice_number"])
        items.append(
            Item(f"ev-sheet-lines-{n + 1}.xlsx", R.line_item_xlsx(invs), "spreadsheet", "xlsx", invs,
                 truths=truths, notes="one row per line item")
        )  # fmt: skip
    for n in range(3):
        invs = regs(3)
        items.append(
            Item(f"ev-sheet-legacy-{n + 1}.xls", R.register_xls(invs, title_rows=n), "spreadsheet", "xls", invs,
                 truths=[sheet_truth(i) for i in invs], notes="BIFF8 .xls")
        )  # fmt: skip

    # -- CSV (10): delimiters x encodings, European numbers with ';'
    csv_specs = [
        (",", "utf-8", False), (";", "utf-8", True), ("\t", "utf-8", False), ("|", "utf-8", False),
        (",", "utf-8-sig", False), (";", "cp1252", True), (",", "utf-16", False), (";", "utf-16", True),
        (",", "cp1252", False), (";", "utf-8-sig", True),
    ]  # fmt: skip
    names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
    for n, (delim, enc, eu) in enumerate(csv_specs):
        invs = regs(rng.randint(2, 4))
        if eu:
            invs = [i.with_(supplier_name=i.supplier_name.replace("Ltd", "GmbH")) for i in invs]
        items.append(
            Item(f"ev-csv-{n + 1:02d}-{names[delim]}-{enc}.csv",
                 R.register_csv(invs, delimiter=delim, encoding=enc, european=eu), "csv", "csv", invs,
                 truths=[sheet_truth(i) for i in invs],
                 notes=f"delimiter {names[delim]}, {enc}{', European numbers and dates' if eu else ''}")
        )  # fmt: skip

    # -- multi-invoice PDFs (2 x 5 invoices)
    for n in range(2):
        invs = [f.invoice(layout=rng.choice(("classic", "modern", "european", "table_heavy"))) for _ in range(5)]
        if n == 1:
            invs[2] = f.invoice(layout="table_heavy", n_items=30)  # one invoice spans two pages
        items.append(
            Item(f"ev-multi-{n + 1}.pdf", R.multi_invoice_pdf(invs), "multi_invoice_pdf", "pdf", invs,
                 layout="mixed", llm_requests=len(invs), notes=f"{len(invs)} invoices, back to back")
        )  # fmt: skip

    # -- planted validation errors
    def planted(name: str, inv: Invoice, rules: tuple[str, ...], group: str | None = None, notes: str = "") -> None:
        items.append(
            Item(f"ev-planted-{name}.pdf", R.invoice_pdf(inv), "planted_error", "pdf", [inv], layout=inv.layout,
                 extra_rules=rules, group=group, llm_requests=1, notes=notes)
        )  # fmt: skip

    for n in range(2):
        inv = f.invoice(layout="classic", missing_customer=False, no_tax=False)
        planted(f"total-{n + 1}", inv.with_(printed_total=inv.total + Decimal("30.00")), ("TOTAL_MISMATCH",),
                notes="printed total = subtotal + tax + 30.00")  # fmt: skip
    for n in range(2):
        inv = f.invoice(layout="modern", n_items=3, missing_customer=False, no_tax=False)
        bumped = inv.subtotal + Decimal("15.00")
        planted(f"linesum-{n + 1}", inv.with_(printed_subtotal=bumped, printed_total=bumped + (inv.tax or 0)),
                ("LINE_SUM_MISMATCH",), notes="subtotal and total consistent, lines sum 15.00 lower")  # fmt: skip
    for n in range(2):
        inv = f.invoice(layout="classic", missing_customer=False, no_tax=False)
        planted(f"nonumber-{n + 1}", inv.with_(invoice_number=None), ("MISSING_FIELD",), notes="no invoice number")
    # A fixed date keeps the dataset deterministic; it is in the future until 2027-12-24 (7-day grace).
    inv = f.invoice(layout="classic", missing_customer=False, no_tax=False, invoice_date=FUTURE)
    planted("future-1", inv, ("FUTURE_DATE",), notes=f"dated {FUTURE.isoformat()}")
    # duplicate pair: same invoice, two layouts; the later upload gets DUPLICATE_INVOICE
    inv = f.invoice(layout="classic", missing_customer=False, no_tax=False)
    planted("dup-a", inv, (), group="dup")
    planted("dup-b", inv.with_(layout="modern"), ("DUPLICATE_INVOICE",), group="dup", notes="re-issued copy of dup-a")
    # PDF <-> PDF conflict: same number and supplier, different total -> both flagged
    inv = f.invoice(layout="classic", missing_customer=False, no_tax=False, n_items=2)
    other = inv.with_(line_items=inv.line_items[:1], subtotal=inv.line_items[0].amount,
                      tax=(inv.line_items[0].amount * (inv.tax_rate or 0)).quantize(Decimal("0.01")))  # fmt: skip
    other = other.with_(total=other.subtotal + (other.tax or 0))
    planted("conflict-a", inv, ("INVOICE_NUMBER_CONFLICT",), group="conflict")
    planted("conflict-b", other, ("INVOICE_NUMBER_CONFLICT",), group="conflict", notes="same number, one line fewer")
    # PDF <-> spreadsheet mismatch: the ledger row has a different total -> both flagged
    inv = f.invoice(layout="classic", missing_customer=False, no_tax=False)
    planted("xdoc-pdf", inv, ("CROSS_DOC_MISMATCH",), group="xdoc")
    ledger_inv = inv.with_(printed_total=inv.total + Decimal("12.50"), printed_subtotal=inv.subtotal + Decimal("12.50"))
    items.append(
        Item("ev-planted-xdoc-ledger.xlsx", R.register_xlsx([("Ledger", [ledger_inv])]), "planted_error", "xlsx",
             [ledger_inv], truths=[sheet_truth(ledger_inv)], extra_rules=("CROSS_DOC_MISMATCH",), group="xdoc",
             notes="ledger total 12.50 higher than xdoc-pdf")
    )  # fmt: skip

    # -- invalid files (8)
    bad = f.invoice()
    base_xlsx = R.register_xlsx([("Invoices", regs(1))])
    for name, data, fmt, code in (
        ("truncated.pdf", R.truncated_pdf(bad), "pdf", "CORRUPT_FILE"),
        ("garbage.pdf", R.garbage_pdf(), "pdf", "CORRUPT_FILE"),
        ("encrypted.pdf", R.encrypted_pdf(bad), "pdf", "PASSWORD_PROTECTED"),
        ("empty.pdf", b"", "pdf", "EMPTY_FILE"),
        ("renamed-exe.pdf", R.fake_exe(), "pdf", "TYPE_MISMATCH"),
        ("random.bin", R.random_binary(rng), "bin", "UNSUPPORTED_TYPE"),
        ("bomb.png", R.png_bomb(), "png", "DECOMPRESSION_BOMB"),
        ("zipbomb.xlsx", R.zip_bomb_xlsx(base_xlsx), "xlsx", "DECOMPRESSION_BOMB"),
    ):
        items.append(Item(f"ev-invalid-{name}", data, "invalid", fmt, error_code=code))

    return finalize(items, rng)


def finalize(items: list[Item], rng: random.Random) -> list[ManifestEntry]:
    if FILES.exists():
        shutil.rmtree(FILES)
    FILES.mkdir(parents=True)
    # split: per category, 30 % dev; grouped files move together
    units: dict[str, list[list[Item]]] = defaultdict(list)
    grouped: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if item.group:
            grouped[item.group].append(item)
        else:
            units[item.category].append([item])
    for members in grouped.values():
        units[members[0].category].append(members)
    split_of: dict[str, str] = {}
    for category in sorted(units):
        bucket = sorted(units[category], key=lambda u: u[0].file)
        rng.shuffle(bucket)
        n_dev = round(len(bucket) * DEV_SHARE)
        for i, unit in enumerate(bucket):
            for member in unit:
                split_of[member.file] = "dev" if i < n_dev else "test"
    entries = []
    for item in items:
        path = FILES / item.file
        path.write_bytes(item.data)
        truths = item.truths if item.truths is not None else [inv.truth() for inv in item.invoices]
        rules: set[str] = set(item.extra_rules)
        for inv, truth in zip(item.invoices, truths, strict=True):
            if item.category in ("spreadsheet", "csv") or item.fmt == "xlsx":
                rules |= sheet_rules(truth)
            else:
                rules |= implied_rules(truth, inv.document_type)
        entries.append(
            ManifestEntry(
                file=item.file,
                sha256=sha256_of(path),
                split=split_of[item.file],  # type: ignore[arg-type]
                format=item.fmt,  # type: ignore[arg-type]
                category=item.category,  # type: ignore[arg-type]
                layout=item.layout,
                degradation=item.degradation,  # type: ignore[arg-type]
                expected={"invoices": [ExpectedInvoice.model_validate(t) for t in truths]},  # type: ignore[arg-type]
                expected_error_code=item.error_code,
                expected_rule_codes=sorted(rules & set(SCORED_RULES)),
                group=item.group,
                llm_requests=item.llm_requests,
                notes=item.notes,
            )
        )
    write_manifest(entries)
    return entries


def main() -> int:
    entries = build()
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for e in entries:
        row = by[e.category]
        row[0] += 1
        row[1] += len(e.expected.invoices)
        row[2] += e.llm_requests
    size = sum((FILES / e.file).stat().st_size for e in entries)
    print(f"{len(entries)} files, {size / 1e6:.1f} MB -> {FILES}")
    for category, (n, invoices, requests) in sorted(by.items()):
        print(f"  {category:18} files={n:3} invoices={invoices:3} llm_requests={requests:3}")
    for split in ("dev", "test"):
        part = [e for e in entries if e.split == split]
        print(f"  split {split}: {len(part)} files, {sum(e.llm_requests for e in part)} planned LLM requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
