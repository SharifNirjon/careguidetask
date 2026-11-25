"""Turn Invoice objects into files: digital/scanned PDFs, images, XLSX/XLS, CSV.

Layouts print the same facts in different visual conventions. Degradation
levels for scans are fixed presets so a breakdown by level is meaningful.
"""

from __future__ import annotations

import csv
import io
import random
import re
import struct
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

import cv2
import numpy as np
import openpyxl
import pymupdf
from PIL import Image

from tools.synth.model import SYMBOLS, Invoice

PAGE_W: Final = 595  # A4 points
PAGE_H: Final = 842
MARGIN: Final = 56
LINES_PER_PAGE: Final = 44

Level = Literal["mild", "medium", "heavy"]


# ------------------------------------------------------------------ formats


def fmt_amount(value: Decimal, *, european: bool = False) -> str:
    sign = "-" if value < 0 else ""
    whole, _, cents = f"{abs(value):.2f}".partition(".")
    groups = f"{int(whole):,}"
    if european:
        return f"{sign}{groups.replace(',', '.')},{cents}"
    return f"{sign}{groups}.{cents}"


def fmt_date(day: date, style: str) -> str:
    if style == "long":
        return f"{day.day} {day.strftime('%B %Y')}"
    if style == "dmy_dots":
        return day.strftime("%d.%m.%Y")
    if style == "slash_ambiguous":
        return day.strftime("%d/%m/%Y")
    return day.isoformat()


def fmt_money(inv: Invoice, value: Decimal) -> str:
    amount = fmt_amount(value, european=inv.european)
    if inv.currency_style == "code":
        return f"{amount} {inv.currency}"
    symbol = SYMBOLS[inv.currency]
    return f"{amount} {symbol}" if inv.european else f"{symbol}{amount}"


def _qty(value: Decimal) -> str:
    return f"{value.normalize():f}"


# ------------------------------------------------------------------ layouts


def _items_block(inv: Invoice, *, wide: bool) -> list[str]:
    eu = inv.european
    if wide:
        rows = [f"{'#':<3}{'Description':<32}{'Qty':>6}{'Unit price':>13}{'Amount':>13}"]
        rows += [
            f"{n:<3}{item.description[:31]:<32}{_qty(item.quantity):>6}"
            f"{fmt_amount(item.unit_price, european=eu):>13}{fmt_amount(item.amount, european=eu):>13}"
            for n, item in enumerate(inv.line_items, 1)
        ]
        return rows
    rows = ["Description                     Qty   Unit price      Amount"]
    rows += [
        f"{item.description:<30}  {_qty(item.quantity):>3}   {fmt_amount(item.unit_price, european=eu):>10}"
        f"  {fmt_amount(item.amount, european=eu):>10}"
        for item in inv.line_items
    ]
    return rows


def _tax_label(inv: Invoice) -> str:
    rate = "" if inv.tax_rate is None else f" {int(inv.tax_rate * 100)}%"
    return f"VAT{rate}"


def _header(inv: Invoice) -> list[str]:
    d = fmt_date(inv.invoice_date, inv.date_style)
    number = inv.invoice_number
    bill_to = [] if inv.customer_name is None else [f"Bill To: {inv.customer_name}"]
    code_line = [f"Currency: {inv.currency}"] if inv.currency_style == "symbol_code" else []
    layout = inv.layout
    if layout == "modern":
        return [
            inv.supplier_name,
            "",
            *([f"Invoice #{number}"] if number else ["Invoice"]),
            f"Issued {d}",
            *([f"Billed to {inv.customer_name}"] if inv.customer_name else []),
            *code_line,
        ]
    if layout == "minimal":
        head = f"No. {number}  ·  {d}" if number else d
        return [inv.supplier_name, head, *([inv.customer_name] if inv.customer_name else []), *code_line]
    if layout == "receipt":
        return [
            inv.supplier_name,
            "RECEIPT",
            *([f"Receipt No: {number}"] if number else []),
            f"Date: {d}",
            *code_line,
        ]
    if layout == "credit_note":
        return [
            "CREDIT NOTE",
            *([f"Credit Note No: {number}"] if number else []),
            f"Date: {d}",
            f"Supplier: {inv.supplier_name}",
            *bill_to,
            *code_line,
        ]
    if layout == "european":
        return [
            inv.supplier_name,
            "INVOICE",
            *([f"Invoice no.: {number}"] if number else []),
            f"Invoice date: {d}",
            *bill_to,
            *code_line,
        ]
    return [
        "INVOICE",
        *([f"Invoice Number: {number}"] if number else []),
        f"Date: {d}",
        f"Supplier: {inv.supplier_name}",
        *bill_to,
        *code_line,
    ]


def _totals(inv: Invoice) -> list[str]:
    m = lambda v: fmt_money(inv, v)  # noqa: E731
    tax = inv.tax
    if inv.layout == "tax_inclusive":
        return [
            f"Net amount {m(inv.shown_subtotal)}",
            f"of which {_tax_label(inv)} {m(tax)}" if tax is not None else "",
            f"Total (incl. VAT) {m(inv.shown_total)}",
        ]
    if inv.layout == "minimal":
        return [*([f"Tax {m(tax)}"] if tax is not None else []), f"Total {m(inv.shown_total)}"]
    if inv.layout == "modern":
        return [
            f"Net {m(inv.shown_subtotal)}",
            *([f"Tax ({_tax_label(inv)}) {m(tax)}"] if tax is not None else []),
            f"Amount due {m(inv.shown_total)}",
        ]
    if inv.layout == "receipt":
        return [
            f"Subtotal {m(inv.shown_subtotal)}",
            *([f"{_tax_label(inv)} {m(tax)}"] if tax is not None else []),
            f"TOTAL {m(inv.shown_total)}",
            "Thank you for your purchase",
        ]
    return [
        f"Subtotal {m(inv.shown_subtotal)}",
        *([f"{_tax_label(inv)} {m(tax)}"] if tax is not None else []),
        f"{'Total credit' if inv.layout == 'credit_note' else 'Total Amount Due'} {m(inv.shown_total)}",
    ]


def layout_pages(inv: Invoice) -> list[list[str]]:
    """Text lines per page. Long item tables continue on extra pages with a
    repeated header and 'Page i of n' counters (what segmentation relies on)."""
    head = _header(inv)
    items = _items_block(inv, wide=inv.layout == "table_heavy")
    totals = _totals(inv)
    body = [*head, "", *items, "", *totals]
    if len(body) + 2 <= LINES_PER_PAGE:
        return [[*body, "", "Page 1 of 1"]]
    rows = items[1:]
    first_room = LINES_PER_PAGE - len(head) - 4
    chunks = [rows[:first_room]]
    rest = rows[first_room:]
    room = LINES_PER_PAGE - 6
    while rest:
        chunks.append(rest[:room])
        rest = rest[room:]
    if len(chunks[-1]) + len(totals) + 4 > LINES_PER_PAGE:
        chunks.append([])
    pages = []
    for i, chunk in enumerate(chunks):
        top = head if i == 0 else [f"{inv.supplier_name} (continued)"]
        lines = [*top, "", items[0], *chunk]
        if i == len(chunks) - 1:
            lines += ["", *totals]
        pages.append([*lines, "", f"Page {i + 1} of {len(chunks)}"])
    return pages


# ------------------------------------------------------------ determinism

FIXED_PDF_META: Final = {"creationDate": "D:20260101000000", "modDate": "D:20260101000000", "producer": "tools.synth"}
FIXED_ZIP_TIME: Final = (2026, 1, 1, 0, 0, 0)


def _pdf_bytes(doc: pymupdf.Document) -> bytes:
    """No timestamps, no random /ID: the same input gives the same bytes."""
    doc.set_metadata(FIXED_PDF_META)
    return doc.tobytes(garbage=3, deflate=True, no_new_id=True)


def _stable_zip(data: bytes) -> bytes:
    """Rewrite an OOXML zip with fixed member times and fixed core.xml dates."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in sorted(src.infolist(), key=lambda i: i.filename):
            body = src.read(item.filename)
            if item.filename == "docProps/core.xml":
                body = re.sub(rb"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", b"2026-01-01T00:00:00Z", body)
            info = zipfile.ZipInfo(item.filename, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            dst.writestr(info, body)
    return out.getvalue()


def _xlsx_bytes(wb: openpyxl.Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return _stable_zip(buf.getvalue())


# ---------------------------------------------------------------------- PDF


def digital_pdf(pages: Sequence[Sequence[str]], *, mono: bool = False, fontsize: float = 10.5) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN
        for line in lines:
            if line:
                page.insert_text((MARGIN, y), line, fontsize=fontsize, fontname="cour" if mono else "helv")
            y += fontsize * 1.6
    return _pdf_bytes(doc)


def invoice_pdf(inv: Invoice) -> bytes:
    return digital_pdf(layout_pages(inv), mono=inv.layout == "table_heavy")


def render_gray(pdf: bytes, page_index: int = 0, dpi: int = 200) -> np.ndarray:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pix = doc[page_index].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].copy()


@dataclass(frozen=True, slots=True)
class Degradation:
    dpi: int
    rotate_deg: float
    blur_sigma: float
    noise_sigma: float
    jpeg_quality: int


DEGRADATIONS: Final[dict[str, Degradation]] = {
    "mild": Degradation(dpi=200, rotate_deg=0.8, blur_sigma=0.0, noise_sigma=5.0, jpeg_quality=90),
    "medium": Degradation(dpi=150, rotate_deg=2.5, blur_sigma=0.8, noise_sigma=12.0, jpeg_quality=60),
    "heavy": Degradation(dpi=110, rotate_deg=5.0, blur_sigma=1.3, noise_sigma=20.0, jpeg_quality=40),
}


def degrade(gray: np.ndarray, level: Level, rng: random.Random) -> np.ndarray:
    preset = DEGRADATIONS[level]
    h, w = gray.shape
    angle = preset.rotate_deg * rng.choice((-1, 1))
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    out = cv2.warpAffine(gray, matrix, (w, h), borderValue=255)
    if preset.blur_sigma:
        out = cv2.GaussianBlur(out, (0, 0), preset.blur_sigma)
    noise = np.random.default_rng(rng.randrange(2**32)).normal(0, preset.noise_sigma, out.shape)
    return np.clip(out.astype(np.float64) + noise, 0, 255).astype(np.uint8)


def _jpeg(gray: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def scanned_pdf(inv: Invoice, level: Level, rng: random.Random) -> bytes:
    """Every page rasterised, degraded and re-embedded as a JPEG (no text layer)."""
    clean = invoice_pdf(inv)
    preset = DEGRADATIONS[level]
    out = pymupdf.open()
    for i in range(pymupdf.open(stream=clean, filetype="pdf").page_count):
        gray = degrade(render_gray(clean, i, preset.dpi), level, rng)
        page = out.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(page.rect, stream=_jpeg(gray, preset.jpeg_quality))
    return _pdf_bytes(out)


def multi_invoice_pdf(invoices: Sequence[Invoice]) -> bytes:
    pages = [page for inv in invoices for page in layout_pages(inv)]
    return digital_pdf(pages)


# ------------------------------------------------------------------- images


def _pil_bytes(img: Image.Image, fmt: str, **kw: object) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def image_file(inv: Invoice, fmt: Literal["PNG", "JPEG"], level: Level, rng: random.Random) -> bytes:
    preset = DEGRADATIONS[level]
    gray = degrade(render_gray(invoice_pdf(inv), 0, preset.dpi), level, rng)
    img = Image.fromarray(gray)
    if fmt == "JPEG":
        return _pil_bytes(img, "JPEG", quality=preset.jpeg_quality, dpi=(preset.dpi, preset.dpi))
    return _pil_bytes(img, "PNG", dpi=(preset.dpi, preset.dpi))


def warped_jpeg(inv: Invoice, rng: random.Random) -> bytes:
    """Mild perspective warp, like a phone photo taken slightly off-axis."""
    gray = render_gray(invoice_pdf(inv), 0, 170)
    h, w = gray.shape
    d = 0.04
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    jitter = [rng.uniform(0, d) for _ in range(4)]
    dst = np.float32(
        [
            [w * jitter[0], h * jitter[1] / 2],
            [w * (1 - jitter[1]), 0],
            [w, h * (1 - jitter[2] / 2)],
            [w * jitter[3], h],
        ]
    )
    warped = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=235)
    return _jpeg(warped, 85)


def multiframe_tiff(invoices: Sequence[Invoice], level: Level, rng: random.Random) -> bytes:
    preset = DEGRADATIONS[level]
    frames = [
        Image.fromarray(degrade(render_gray(invoice_pdf(inv), 0, preset.dpi), level, rng)) for inv in invoices
    ]
    return _pil_bytes(
        frames[0], "TIFF", save_all=True, append_images=frames[1:], compression="tiff_lzw", dpi=(preset.dpi,) * 2
    )


# ------------------------------------------------------------- spreadsheets

REGISTER_HEADERS: Final = ("Invoice No", "Date", "Supplier", "Customer", "Subtotal", "VAT", "Total", "Currency")


def register_rows(invoices: Sequence[Invoice], *, european: bool = False) -> list[list[str]]:
    rows = []
    for inv in invoices:
        rows.append(
            [
                inv.invoice_number or "",
                inv.invoice_date.strftime("%d.%m.%Y") if european else inv.invoice_date.isoformat(),
                inv.supplier_name,
                inv.customer_name or "",
                fmt_amount(inv.shown_subtotal, european=european),
                "" if inv.tax is None else fmt_amount(inv.tax, european=european),
                fmt_amount(inv.shown_total, european=european),
                inv.currency,
            ]
        )
    return rows


def register_xlsx(
    sheets: Sequence[tuple[str, Sequence[Invoice]]],
    *,
    title_rows: int = 0,
    merge_currency: bool = False,
    notes_sheet: bool = False,
    numeric_cells: bool = False,
) -> bytes:
    """One invoice per row. title_rows pushes the header down; merge_currency
    merges the (identical) currency column vertically; notes_sheet adds an
    auxiliary sheet that is not an invoice register."""
    wb = openpyxl.Workbook()
    first = True
    for name, invoices in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        assert ws is not None
        ws.title = name
        for i in range(title_rows):
            ws.append([f"{invoices[0].supplier_name} - invoice register" if i == 0 else ""])
        if title_rows:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
        ws.append(list(REGISTER_HEADERS))
        header_row = ws.max_row
        for inv, row in zip(invoices, register_rows(invoices), strict=True):
            if numeric_cells:
                ws.append(
                    [
                        row[0],
                        inv.invoice_date,
                        row[2],
                        row[3] or None,
                        float(inv.shown_subtotal),
                        None if inv.tax is None else float(inv.tax),
                        float(inv.shown_total),
                        row[7],
                    ]
                )
            else:
                ws.append([cell or None for cell in row])
        last = ws.max_row
        if merge_currency and len({inv.currency for inv in invoices}) == 1 and last > header_row + 1:
            ws.merge_cells(start_row=header_row + 1, start_column=8, end_row=last, end_column=8)
    if notes_sheet:
        notes = wb.create_sheet("Notes")
        notes.append(["Note", "Owner"])
        notes.append(["Q3 figures reviewed", "Finance"])
    return _xlsx_bytes(wb)


def line_item_xlsx(invoices: Sequence[Invoice]) -> bytes:
    """One row per line item, invoice-level columns repeated on each row."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Lines"
    ws.append(["Invoice No", "Date", "Supplier", "Customer", "Description", "Qty", "Unit Price", "Amount",
               "Subtotal", "VAT", "Total", "Currency"])  # fmt: skip
    for inv in invoices:
        for item in inv.line_items:
            ws.append(
                [
                    inv.invoice_number,
                    inv.invoice_date.isoformat(),
                    inv.supplier_name,
                    inv.customer_name,
                    item.description,
                    _qty(item.quantity),
                    fmt_amount(item.unit_price),
                    fmt_amount(item.amount),
                    fmt_amount(inv.shown_subtotal),
                    None if inv.tax is None else fmt_amount(inv.tax),
                    fmt_amount(inv.shown_total),
                    inv.currency,
                ]
            )
    return _xlsx_bytes(wb)


def register_xls(invoices: Sequence[Invoice], *, title_rows: int = 0) -> bytes:
    import xlwt  # dev-only dependency (writes BIFF8)

    book = xlwt.Workbook()
    sheet = book.add_sheet("Invoices")
    for i in range(title_rows):
        sheet.write(i, 0, "Invoice export" if i == 0 else "")
    sheet_rows = [list(REGISTER_HEADERS), *register_rows(invoices)]
    for r, row in enumerate(sheet_rows, start=title_rows):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def register_csv(invoices: Sequence[Invoice], *, delimiter: str, encoding: str, european: bool) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(REGISTER_HEADERS)
    writer.writerows(register_rows(invoices, european=european))
    return buf.getvalue().encode(encoding)


# ---------------------------------------------------------------- bad files


def truncated_pdf(inv: Invoice) -> bytes:
    data = invoice_pdf(inv)
    return data[: len(data) // 10]


def garbage_pdf() -> bytes:
    return b"%PDF-1.7\n" + b"\x00garbage" * 64


def encrypted_pdf(inv: Invoice) -> bytes:
    doc = pymupdf.open(stream=invoice_pdf(inv), filetype="pdf")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")


def fake_exe() -> bytes:
    return b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + b"\x00" * 200


def random_binary(rng: random.Random) -> bytes:
    return bytes(rng.randrange(256) for _ in range(2048))


def png_bomb(width: int = 50_000, height: int = 50_000) -> bytes:
    data = bytearray(_pil_bytes(Image.new("L", (1, 1), 255), "PNG"))
    start = 8 + 4
    data[start + 4 : start + 12] = struct.pack(">II", width, height)
    crc = zlib.crc32(bytes(data[start : start + 17])) & 0xFFFFFFFF
    data[start + 17 : start + 21] = struct.pack(">I", crc)
    return bytes(data)


def zip_bomb_xlsx(base: bytes, member_mb: int = 64) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(base)) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/media/padding.bin", b"\0" * (member_mb * 1024 * 1024))
    return _stable_zip(buf.getvalue())
