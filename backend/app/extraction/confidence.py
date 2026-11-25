"""Confidence fusion. LLM self-confidence alone is poorly calibrated, so a field
is only as confident as its weakest signal."""

from __future__ import annotations

from collections.abc import Mapping


def field_confidence(
    llm_confidence: float,
    grounding_score: float | None,
    ocr_confidence: float | None,
    *,
    ambiguity_factor: float | None = None,
) -> float:
    """min(LLM, grounding, OCR of the cited unit), scaled down for ambiguous parses."""
    signals = [llm_confidence]
    if grounding_score is not None:
        signals.append(grounding_score)
    if ocr_confidence is not None:
        signals.append(ocr_confidence)
    confidence = min(signals)
    if ambiguity_factor is not None:
        confidence *= ambiguity_factor
    return round(max(0.0, min(confidence, 1.0)), 3)


def overall_confidence(field_scores: Mapping[str, float | None], weights: Mapping[str, float]) -> float:
    """Weighted mean over the required fields; a missing required field counts as 0."""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    weighted = sum(weight * (field_scores.get(name) or 0.0) for name, weight in weights.items())
    return round(weighted / total_weight, 3)
