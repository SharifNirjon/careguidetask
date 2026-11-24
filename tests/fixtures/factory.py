"""Programmatic test fixtures (nothing large is committed).

    python -m tests.fixtures.factory            # writes tests/fixtures/generated/

Every function returns bytes; `write_all()` writes the full set, including the
bad files, so `python -m app.cli ingest tests/fixtures/generated --sync` covers
every type and every expected failure.
"""

from __future__ import annotations

import csv
import io
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Final

import cv2
import numpy as np
import openpyxl
import pymupdf
from PIL import Image

GENERATED_DIR: Final = Path(__file__).parent / "generated"
COMMITTED_XLS: Final = Path(__file__).parent / "invoice.xls"
FONT_SIZE: Final = 14
LINE_HEIGHT: Final = 22
PAGE_MARGIN: Final = 72
RNG_SEED: Final = 42


# --------------------------------------------------------------------- text


def invoice_lines(number: str, page: int = 1, pages: int = 1, *, totals: bool = True) -> list[str]:
    lines = [
        "INVOICE",
        f"Invoice Number: {number}",
        "Date: 2024-03-05",
        "Bill To: Northwind Clinic, 12 Harbour Road",
        "Supplier: ACME Supplies Ltd",
        "",
        "Description            Qty    Unit Price    Amount",
        "Nitrile gloves (box)    10         12.50    125.00",
        "Surgical masks           5         20.00    100.00",
    ]
    if totals:
        lines += ["", "Subtotal 225.00", "VAT 20% 45.00", "Total Amount Due 270.00 EUR"]
    lines += ["", f"Page {page} of {pages}"]
    return lines


def _write_lines(page: pymupdf.Page, lines: list[str], *, render_mode: int = 0) -> None:
    y = PAGE_MARGIN
    for line in lines:
        if line:
            page.insert_text((PAGE_MARGIN, y), line, fontsize=FONT_SIZE, render_mode=render_mode)
        y += LINE_HEIGHT


# ---------------------------------------------------------------------- PDF


def digital_pdf(pages: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        _write_lines(doc.new_page(), lines)
    return doc.tobytes()


def render_page_image(lines: list[str], dpi: int = 200) -> np.ndarray:
    """Grayscale 'scan' of a digital page (uint8 numpy array)."""
    doc = pymupdf.open(stream=digital_pdf([lines]), filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].copy()


def degrade(gray: np.ndarray, *, rotate_deg: float = 3.0, noise_sigma: float = 12.0) -> np.ndarray:
    """Make a clean render look scanned: slight rotation + sensor noise."""
    h, w = gray.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), rotate_deg, 1.0)
    rotated = cv2.warpAffine(gray, matrix, (w, h), borderValue=255)
    noise = np.random.default_rng(RNG_SEED).normal(0, noise_sigma, rotated.shape)
    return np.clip(rotated.astype(np.float64) + noise, 0, 255).astype(np.uint8)


def _png_bytes(gray: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", gray)
    assert ok
    return buf.tobytes()


def _image_page(doc: pymupdf.Document, gray: np.ndarray) -> pymupdf.Page:
    page = doc.new_page()
    page.insert_image(page.rect, stream=_png_bytes(gray))
    return page


def scanned_pdf(lines: list[str] | None = None) -> bytes:
    doc = pymupdf.open()
    _image_page(doc, degrade(render_page_image(lines or invoice_lines("INV-2024-001"))))
    return doc.tobytes()


def mixed_pdf() -> bytes:
    """Page 0 digital, page 1 scanned."""
    doc = pymupdf.open(
        stream=digital_pdf([invoice_lines("INV-2024-001", 1, 2, totals=False)]), filetype="pdf"
    )
    _image_page(doc, degrade(render_page_image(invoice_lines("INV-2024-001", 2, 2))))
    return doc.tobytes()


def broken_font_pdf() -> bytes:
    """Scanned page with a garbage text layer ('(cid:NN)' artifacts) on top."""
    doc = pymupdf.open()
    page = _image_page(doc, render_page_image(invoice_lines("INV-2024-777")))
    garbage = [" ".join(f"(cid:{n})" for n in range(i, i + 12)) for i in range(0, 120, 12)]
    _write_lines(page, garbage, render_mode=3)  # invisible, like a bad OCR/font layer
    return doc.tobytes()


def encrypted_pdf() -> bytes:
    doc = pymupdf.open(stream=digital_pdf([invoice_lines("INV-1")]), filetype="pdf")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")


def truncated_pdf(fraction: float = 0.1) -> bytes:
    """Cut so early that MuPDF cannot recover any page (milder cuts get repaired)."""
    data = digital_pdf([invoice_lines("INV-1")])
    return data[: int(len(data) * fraction)]


def repairable_pdf() -> bytes:
    """Truncated, but MuPDF's repair recovers the (now empty) page."""
    return truncated_pdf(0.2)


def multi_invoice_pdf(
    n_pages: int = 30, pages_per_invoice: int = 3, blank_pages: frozenset[int] = frozenset()
) -> bytes:
    """n_pages digital pages; consecutive groups form invoices; some pages blank."""
    pages: list[list[str]] = []
    for i in range(n_pages):
        if i in blank_pages:
            pages.append([])
            continue
        inv, page_no = divmod(i, pages_per_invoice)
        pages.append(
            invoice_lines(
                f"INV-{1000 + inv}", page_no + 1, pages_per_invoice, totals=page_no == pages_per_invoice - 1
            )
        )
    return digital_pdf(pages)


# ------------------------------------------------------------------- images


def _encode(img: Image.Image, fmt: str, **kw: object) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def exif_rotated_jpeg() -> bytes:
    """Pixels stored rotated 90° CW, EXIF Orientation=6 tells viewers to undo it."""
    upright = Image.fromarray(render_page_image(invoice_lines("INV-2024-042"), dpi=150))
    stored = upright.rotate(-90, expand=True)  # 90° clockwise
    exif = Image.Exif()
    exif[0x0112] = 6
    return _encode(stored, "JPEG", exif=exif.tobytes(), quality=92, dpi=(150, 150))


def blurry_png(sigma: float = 2.0) -> bytes:
    gray = render_page_image(invoice_lines("INV-2024-043"), dpi=200)
    return _encode(Image.fromarray(cv2.GaussianBlur(gray, (0, 0), sigma)), "PNG")


def sharp_png() -> bytes:
    return _encode(Image.fromarray(render_page_image(invoice_lines("INV-2024-043"), dpi=200)), "PNG")


def multipage_tiff(frames: int = 3) -> bytes:
    images = [Image.fromarray(render_page_image(invoice_lines(f"INV-30{i}"), dpi=150)) for i in range(frames)]
    return _encode(images[0], "TIFF", save_all=True, append_images=images[1:], compression="tiff_lzw")


def png_bomb(width: int = 50_000, height: int = 50_000) -> bytes:
    """Tiny PNG whose header claims 2.5 gigapixels (IHDR patched, CRC fixed)."""
    data = bytearray(_encode(Image.new("L", (1, 1), 255), "PNG"))
    ihdr_start = 8 + 4  # signature + length
    data[ihdr_start + 4 : ihdr_start + 12] = struct.pack(">II", width, height)
    crc = zlib.crc32(bytes(data[ihdr_start : ihdr_start + 4 + 13])) & 0xFFFFFFFF
    data[ihdr_start + 17 : ihdr_start + 21] = struct.pack(">I", crc)
    return bytes(data)


# ------------------------------------------------------------- spreadsheets


def invoice_xlsx() -> bytes:
    """2 visible sheets + 1 hidden; title rows, header on row 4, merged cells, formula."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws["A1"] = "ACME Supplies Ltd - Invoice register"
    ws.merge_cells("A1:E1")
    ws["A2"] = "Exported 2024-03-31"
    ws.append([])
    ws.append(["Invoice No", "Date", "Customer", "Amount", "Currency"])  # row 4
    ws.append(["00123", "2024-03-05", "Northwind Clinic", "1.234,56", "EUR"])
    ws.append(["00124", "2024-03-09", "Northwind Clinic", "99.00", "EUR"])
    ws.append(["00125", "2024-03-12", "Contoso Pharmacy", 450.5, "EUR"])
    ws["F5"] = "=D7*2"  # formula without a cached value (openpyxl never computes)
    ws.merge_cells("E5:E7")  # currency merged down; must be forward-filled
    ws.append([])
    ws.append([])
    ws.append(["Notes", "Status"])  # a second table block
    ws.append(["Late fee waived", "open"])

    summary = wb.create_sheet("Summary")
    summary.append(["Customer", "Total"])
    summary.append(["Northwind Clinic", "1333.56"])

    hidden = wb.create_sheet("Lookup")
    hidden.append(["Code", "Meaning"])
    hidden.append(["EUR", "Euro"])
    hidden.sheet_state = "hidden"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def zip_bomb_xlsx(member_mb: int = 64) -> bytes:
    """Valid-looking xlsx with one highly compressible huge member."""
    buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(invoice_xlsx())) as src,
        zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/media/padding.bin", b"\0" * (member_mb * 1024 * 1024))
    return buf.getvalue()


def macro_xlsm() -> bytes:
    buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(invoice_xlsx())) as src,
        zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/vbaProject.bin", b"\xd0\xcf\x11\xe0fake-vba")
    return buf.getvalue()


# ---------------------------------------------------------------------- CSV


CSV_ROWS: Final = [
    ["Invoice", "Supplier", "Amount", "Date"],
    ["00123", "Café Müller", "1.234,56", "05.03.2024"],
    ["00124", "Bäckerei Groß", "99,00", "09.03.2024"],
]


def csv_bytes(
    *, delimiter: str = ";", encoding: str = "cp1252", bom: bool = False, rows: list[list[str]] | None = None
) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\r\n").writerows(rows or CSV_ROWS)
    data = buf.getvalue().encode(encoding)
    return (b"\xef\xbb\xbf" + data) if bom else data


def malformed_csv() -> bytes:
    rows = [*CSV_ROWS, ["00125", "Too", "Many", "Fields", "Here"], ["00126"]]
    return csv_bytes(delimiter=",", encoding="utf-8", rows=rows)


# --------------------------------------------------------------------- set


def all_fixtures() -> dict[str, bytes]:
    files = {
        "digital_invoice.pdf": digital_pdf(
            [invoice_lines("INV-2024-001", 1, 2, totals=False), invoice_lines("INV-2024-001", 2, 2)]
        ),
        "scanned_invoice.pdf": scanned_pdf(),
        "mixed_invoice.pdf": mixed_pdf(),
        "broken_font.pdf": broken_font_pdf(),
        "multi_invoice_30p.pdf": multi_invoice_pdf(),
        "encrypted.pdf": encrypted_pdf(),
        "truncated.pdf": truncated_pdf(),
        "repairable.pdf": repairable_pdf(),
        "empty.pdf": b"",
        "renamed_exe.pdf": b"MZ\x90\x00" + b"\x00" * 60 + b"This program cannot be run in DOS mode",
        "exif_rotated.jpg": exif_rotated_jpeg(),
        "blurry.png": blurry_png(),
        "three_frames.tiff": multipage_tiff(),
        "bomb.png": png_bomb(),
        "invoices.xlsx": invoice_xlsx(),
        "zip_bomb.xlsx": zip_bomb_xlsx(),
        "macros.xlsm": macro_xlsm(),
        "semicolon_cp1252.csv": csv_bytes(),
        "utf8_bom.csv": csv_bytes(delimiter=",", encoding="utf-8", bom=True),
        "utf16.csv": csv_bytes(delimiter="\t", encoding="utf-16"),
        "malformed.csv": malformed_csv(),
    }
    if COMMITTED_XLS.exists():
        files["invoice.xls"] = COMMITTED_XLS.read_bytes()
    return files


def write_all(target: Path = GENERATED_DIR) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in all_fixtures().items():
        path = target / name
        path.write_bytes(data)
        written.append(path)
    return written


if __name__ == "__main__":
    out = write_all(Path(sys.argv[1]) if len(sys.argv) > 1 else GENERATED_DIR)
    sys.stdout.write(f"wrote {len(out)} fixtures to {out[0].parent}\n")
