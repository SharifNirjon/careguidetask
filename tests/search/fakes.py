"""Deterministic embedding client for tests: hashed bag-of-words vectors.

Each word adds +/-1 to one of EMBEDDING_DIM buckets (chosen by sha256), then
the vector is L2-normalized. Texts sharing words are close in cosine space,
unrelated texts are near-orthogonal: enough to test ranking, filters and
space isolation without a network. Each space uses a different hash salt, so
its vectors are NOT comparable with another space's - like real models.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Collection
from dataclasses import dataclass, field

from app.config import EMBEDDING_DIM
from app.llm.budget import QuotaStatus
from app.llm.embeddings import EmbeddingResult, InputType
from app.llm.errors import LLMError, LLMUnavailableError
from app.search.keyword import split_codes

_WORD = re.compile(r"[^\W_]+")
PRIMARY = "nemotron-3.5-8b"
FALLBACK = "nemotron-3-1b"


def bag_of_words(text: str, salt: str = PRIMARY) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for word in _WORD.findall(split_codes(text).casefold()):
        digest = hashlib.sha256(f"{salt}:{word}".encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        vector[bucket] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else [1.0 / math.sqrt(EMBEDDING_DIM)] * EMBEDDING_DIM


@dataclass
class FakeEmbeddingClient:
    spaces: list[str] = field(default_factory=lambda: [PRIMARY, FALLBACK])  # in fallback order
    fail_with: LLMError | None = None
    down: set[str] = field(default_factory=set)  # spaces whose model is "unavailable"
    calls: list[tuple[int, InputType, str]] = field(default_factory=list)  # (inputs, input_type, space)

    def embed(
        self, texts: list[str], *, input_type: InputType, spaces: Collection[str] | None = None
    ) -> EmbeddingResult:
        if self.fail_with is not None:
            raise self.fail_with
        usable = [s for s in self.spaces if (spaces is None or s in spaces) and s not in self.down]
        if not usable:
            raise LLMUnavailableError({s: "down" for s in self.spaces})
        space = usable[0]
        self.calls.append((len(texts), input_type, space))
        return EmbeddingResult(
            vectors=[bag_of_words(t, space) for t in texts],
            model_used=f"fake-{space}",
            space=space,
            dims=EMBEDDING_DIM,
            is_primary_space=space == self.spaces[0],
            truncated_inputs=0,
        )


@dataclass
class StaticQuota:
    """Stands in for DailyQuota: a fixed daily limit (EmbedBudget only reads it)."""

    limit: int
    used: int = 0

    def status(self) -> QuotaStatus:
        return QuotaStatus(self.used, self.limit)
