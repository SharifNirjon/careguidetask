"""Grounding: a value is kept only if its evidence really is in the cited unit.

The check is (1) the evidence matches the cited unit's text, case- and
whitespace-insensitively, fuzzily enough to tolerate OCR noise
(rapidfuzz partial ratio >= GROUNDING_MIN_SCORE), and (2) the raw value
occurs inside that evidence. A value that fails is not dropped: the caller
nulls it and keeps it as `rejected_value` so validation can flag it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rapidfuzz import fuzz

from app.extraction.schemas import ExtractedField

_WS: Final = re.compile(r"\s+")
PERCENT: Final = 100.0


class GroundingFailure(StrEnum):
    NO_EVIDENCE = "no_evidence"
    UNKNOWN_UNIT = "unknown_unit"
    EVIDENCE_NOT_IN_UNIT = "evidence_not_in_unit"
    VALUE_NOT_IN_EVIDENCE = "value_not_in_evidence"


@dataclass(frozen=True, slots=True)
class Grounding:
    grounded: bool
    score: float  # 0..1; 1.0 = exact (normalized) substring
    failure: GroundingFailure | None = None


def _norm(text: str) -> str:
    return _WS.sub(" ", text.casefold()).strip()


def ground(field: ExtractedField, unit_texts: dict[int, str], *, min_score: float) -> Grounding:
    """`unit_texts` holds only the units of the segment being extracted, so
    evidence cited from another segment (or a made-up unit) fails."""
    if field.evidence is None or not field.evidence.strip():
        return Grounding(False, 0.0, GroundingFailure.NO_EVIDENCE)
    if field.unit_index is None or field.unit_index not in unit_texts:
        return Grounding(False, 0.0, GroundingFailure.UNKNOWN_UNIT)
    evidence = _norm(field.evidence)
    unit = _norm(unit_texts[field.unit_index])
    score = 1.0 if evidence in unit else fuzz.partial_ratio(evidence, unit) / PERCENT
    if score < min_score:
        return Grounding(False, score, GroundingFailure.EVIDENCE_NOT_IN_UNIT)
    if field.raw_value is not None and _norm(field.raw_value) not in evidence:
        return Grounding(False, score, GroundingFailure.VALUE_NOT_IN_EVIDENCE)
    return Grounding(True, score)
