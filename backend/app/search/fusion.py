"""Reciprocal Rank Fusion and result aggregation. Pure.

RRF: score(d) = sum over lists L of 1 / (k + rank_L(d)), rank starting at 1.
It uses ranks only, so a ts_rank_cd of 0.07 and a cosine similarity of 0.83
never have to be put on one scale - their distributions are unrelated, and
any normalization (min-max, z-score) would be fragile per query. k (60, from
Cormack et al. 2009) damps the head: rank 1 vs 2 matters less than "present
in both lists" vs "present in one".

Hits are aggregated to RESULT keys (document, extraction): a list contributes
the best rank of any of the key's chunks, so a long invoice with many matching
chunks isn't counted many times.
"""

from __future__ import annotations

import uuid
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from app.search.types import MatchType

K = TypeVar("K", bound=Hashable)
ResultKey = tuple[uuid.UUID, uuid.UUID | None]  # (document_id, extraction_id)


def collapse(keys: Sequence[K]) -> list[K]:
    """Ranked keys with duplicates removed, keeping each key's best (first) rank."""
    return list(dict.fromkeys(keys))


def rrf(lists: Sequence[Sequence[K]], *, k: int) -> dict[K, float]:
    scores: dict[K, float] = {}
    for ranked in lists:
        for rank, key in enumerate(collapse(ranked), start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


@dataclass(slots=True)
class Candidate:
    key: ResultKey
    score: float
    keyword: bool = False
    semantic: bool = False
    exact_number: bool = False
    # best evidence per kind, for the snippet: (chunk id | None, unit index)
    keyword_chunk: uuid.UUID | None = None
    semantic_chunk: uuid.UUID | None = None
    field_match: str | None = None
    unit_index: int | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def match_type(self) -> MatchType:
        if self.keyword and self.semantic:
            return MatchType.BOTH
        return MatchType.SEMANTIC if self.semantic else MatchType.KEYWORD


def ideal_score(n_lists: int, k: int) -> float:
    """What a key ranked first in every list would get: used to map RRF to [0, 1]."""
    return n_lists / (k + 1) if n_lists else 1.0


def normalized(score: float, n_lists: int, k: int) -> float:
    return round(min(1.0, score / ideal_score(n_lists, k)), 4)
