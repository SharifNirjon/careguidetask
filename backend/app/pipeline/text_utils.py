"""Text hygiene shared by every parser."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.types import unit_warning

# C0/C1 control characters except TAB (\x09) and LF (\x0a). NUL must go:
# PostgreSQL TEXT cannot store it.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LINE_SEPARATORS = re.compile(r"\r\n?|[  \x0b\x0c\x85]")
_INLINE_WS = re.compile(r"[^\S\n]+")  # whitespace other than newline
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_CID_ARTIFACT = re.compile(r"\(cid:\d+\)")
REPLACEMENT_CHAR = "�"


@dataclass(slots=True)
class CleanText:
    text: str
    warnings: list[dict[str, Any]] = field(default_factory=list)


def clean_text(raw: str | None, max_chars: int) -> CleanText:
    """Normalize extracted text for storage and downstream LLM use.

    - Unicode NFKC (ligatures, full-width digits, NBSP -> plain forms)
    - strip NUL and other control characters
    - collapse runs of inline whitespace, trim line ends, but KEEP line breaks
      (layout carries meaning for extraction); at most one blank line in a row
    - cap at `max_chars`, recording a TEXT_TRUNCATED warning
    """
    if not raw:
        return CleanText("")
    text = unicodedata.normalize("NFKC", raw)
    text = _LINE_SEPARATORS.sub("\n", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _INLINE_WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _EXCESS_BLANK_LINES.sub("\n\n", text).strip()

    warnings: list[dict[str, Any]] = []
    if len(text) > max_chars:
        warnings.append(
            unit_warning(
                "TEXT_TRUNCATED",
                f"Text truncated to {max_chars} characters",
                original_chars=len(text),
            )
        )
        text = text[:max_chars]
    return CleanText(text, warnings)


@dataclass(frozen=True, slots=True)
class TextQuality:
    chars: int  # non-whitespace characters
    alnum_ratio: float
    replacement_ratio: float
    cid_artifacts: int

    def is_garbage(self, min_alnum_ratio: float, max_replacement_ratio: float) -> bool:
        """Broken font encoding / unusable text layer -> caller should OCR instead."""
        if self.chars == 0:
            return False
        return (
            self.cid_artifacts > 0
            or self.alnum_ratio < min_alnum_ratio
            or self.replacement_ratio > max_replacement_ratio
        )


def assess_text(text: str) -> TextQuality:
    """Cheap signals that a PDF text layer is garbage (e.g. missing ToUnicode maps)."""
    visible = [c for c in text if not c.isspace()]
    n = len(visible)
    if n == 0:
        return TextQuality(0, 0.0, 0.0, 0)
    alnum = sum(1 for c in visible if c.isalnum())
    replacement = sum(1 for c in visible if c == REPLACEMENT_CHAR)
    return TextQuality(
        chars=n,
        alnum_ratio=alnum / n,
        replacement_ratio=replacement / n,
        cid_artifacts=len(_CID_ARTIFACT.findall(text)),
    )


def cap_text(text: str, max_chars: int, warnings: list[dict[str, Any]]) -> str:
    """Truncate already-clean text (e.g. a rendered table) with a warning."""
    if len(text) <= max_chars:
        return text
    warnings.append(
        unit_warning("TEXT_TRUNCATED", f"Text truncated to {max_chars} characters", original_chars=len(text))
    )
    return text[:max_chars]
