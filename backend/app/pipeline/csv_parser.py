"""CSV parsing: encoding detection, delimiter sniffing, raw-string rows."""

from __future__ import annotations

import codecs
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from charset_normalizer import from_bytes

from app.config import PipelineSettings
from app.models.enums import ErrorCode, ExtractionMethod, UnitType
from app.pipeline.errors import PermanentError
from app.pipeline.tables import build_structured, render_text
from app.pipeline.text_utils import cap_text, clean_text
from app.pipeline.types import ParsedUnit, unit_warning

DELIMITERS: Final = (",", ";", "\t", "|")
SNIFF_SAMPLE_BYTES: Final = 64 * 1024
SNIFF_SAMPLE_LINES: Final = 50
# A delimiter must split at least this share of sample lines into the same
# number (>1) of fields to be accepted.
SNIFF_MIN_CONSISTENCY: Final = 0.6

_BOMS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass(frozen=True, slots=True)
class Encoding:
    name: str
    bom: bool


def detect_encoding(sample: bytes) -> Encoding | None:
    """BOM first, then strict UTF-8, then charset-normalizer. None = not text."""
    for bom, name in _BOMS:
        if sample.startswith(bom):
            return Encoding(name, bom=True)
    try:
        sample.decode("utf-8")
        return Encoding("utf-8", bom=False)
    except UnicodeDecodeError as exc:
        # A multi-byte char cut at the end of the sample is still UTF-8.
        if exc.start >= len(sample) - 3:
            return Encoding("utf-8", bom=False)
    if b"\x00" in sample:
        return None  # binary (UTF-16 without BOM is not supported)
    best = from_bytes(sample).best()
    return Encoding(best.encoding, bom=False) if best else None


def sniff_delimiter(text: str) -> str | None:
    """Pick the delimiter that splits most sample lines into a consistent width."""
    lines = [ln for ln in text.splitlines()[:SNIFF_SAMPLE_LINES] if ln.strip()]
    if not lines:
        return None
    best: tuple[float, int, str] | None = None
    for delim in DELIMITERS:
        widths = [len(row) for row in csv.reader(lines, delimiter=delim)]
        width, count = Counter(widths).most_common(1)[0]
        if width < 2:
            continue
        score = (count / len(lines), width, delim)
        if score[0] >= SNIFF_MIN_CONSISTENCY and (best is None or score[:2] > best[:2]):
            best = score
    return best[2] if best else None


def looks_like_csv(sample: bytes) -> bool:
    encoding = detect_encoding(sample)
    if encoding is None:
        return False
    text = sample.decode(encoding.name, errors="replace")
    return sniff_delimiter(text) is not None


def parse_csv(path: Path, settings: PipelineSettings) -> ParsedUnit:
    """Parse a CSV file into one unit (unit_type=csv). Values stay raw strings."""
    with open(path, "rb") as fh:
        sample = fh.read(SNIFF_SAMPLE_BYTES)
    encoding = detect_encoding(sample)
    if encoding is None:
        raise PermanentError(ErrorCode.CORRUPT_FILE, details={"reason": "undecodable text"})
    delimiter = sniff_delimiter(sample.decode(encoding.name, errors="replace"))
    if delimiter is None:
        raise PermanentError(ErrorCode.CORRUPT_FILE, details={"reason": "no delimiter detected"})

    warnings = (
        []
        if encoding.name.startswith("utf-8")
        else [unit_warning("ENCODING_DETECTED", f"Decoded as {encoding.name}", encoding=encoding.name)]
    )
    csv.field_size_limit(settings.max_unit_text_chars)
    grid: list[list[str]] = []
    malformed = 0
    truncated = False
    replacements = 0
    with open(path, encoding=encoding.name, errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error:
                malformed += 1
                continue
            if len(grid) >= settings.max_sheet_rows:
                truncated = True
                break
            cleaned = []
            for v in row:
                replacements += v.count("�")
                cleaned.append(clean_text(v, settings.max_unit_text_chars).text)
            grid.append(cleaned)

    if grid:
        expected = Counter(len(r) for r in grid).most_common(1)[0][0]
        malformed += sum(1 for r in grid if r and len(r) != expected)
    if malformed:
        warnings.append(unit_warning("MALFORMED_ROWS", f"{malformed} malformed rows", count=malformed))
    if replacements:
        warnings.append(
            unit_warning("DECODE_ERRORS", f"{replacements} undecodable characters", count=replacements)
        )
    if truncated:
        warnings.append(
            unit_warning(
                "ROWS_TRUNCATED",
                f"Only the first {settings.max_sheet_rows} rows were read",
                max_rows=settings.max_sheet_rows,
            )
        )

    structured = build_structured(grid, list(range(1, len(grid) + 1)), hidden=False, warnings=warnings)
    structured["encoding"] = encoding.name
    structured["delimiter"] = delimiter
    text = cap_text(render_text("CSV", structured), settings.max_unit_text_chars, warnings)
    return ParsedUnit(
        unit_type=UnitType.CSV,
        unit_index=0,
        extraction_method=ExtractionMethod.SPREADSHEET,
        text=text,
        structured_data=structured,
        warnings=list(warnings),
    )
