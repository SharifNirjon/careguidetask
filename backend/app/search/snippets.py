"""Snippets as structured segments: [{text, highlight}] - never HTML.

The client renders segments as text, so document content (untrusted: it came
from a PDF or a spreadsheet) can't inject markup by construction; there is
nothing to escape and nothing to forget to escape.

Keyword hits use ts_headline (run only for the returned page: it re-parses
the chunk) with private-use sentinels, which this module turns into
segments. Semantic hits have no matched terms in the index sense, so the
best window is the one sharing the most query words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from app.search.keyword import split_codes
from app.search.types import Segment

START: Final = "\ue000"
STOP: Final = "\ue001"
# ts_headline drops anything shaped like a markup tag ("<b>", "<script>"). Text is
# shown verbatim (as text, so it is harmless), so '<' is masked before ts_headline
# and restored here.
LESS_THAN: Final = "\ue002"
SENTINELS: Final = (START, STOP)
HEADLINE_OPTIONS: Final = (
    f"StartSel={START}, StopSel={STOP}, MaxWords=30, MinWords=12, ShortWord=2, "
    'MaxFragments=2, FragmentDelimiter=" … "'
)
WINDOW_WORDS: Final = 30
WINDOW_STRIDE: Final = 10
_WORD: Final = re.compile(r"[^\W_]+", re.UNICODE)
_QUOTED_OR_WORD: Final = re.compile(r'-?"[^"]*"?|-?\S+')
STOP_TERMS: Final = frozenset({"or", "and"})


def strip_sentinels(text: str) -> str:
    return text.replace(START, "").replace(STOP, "").replace(LESS_THAN, "<")


def from_headline(marked: str) -> tuple[Segment, ...]:
    """ts_headline output with sentinels -> segments (unbalanced markers are tolerated)."""
    segments: list[Segment] = []
    highlight = False
    buffer = ""
    for ch in marked.replace(LESS_THAN, "<"):
        if ch in SENTINELS:
            if buffer:
                segments.append(Segment(buffer, highlight))
            buffer, highlight = "", ch == START
        else:
            buffer += ch
    if buffer:
        segments.append(Segment(buffer, highlight))
    return _merge(segments)


def _merge(segments: Iterable[Segment]) -> tuple[Segment, ...]:
    merged: list[Segment] = []
    for seg in segments:
        if merged and merged[-1].highlight == seg.highlight:
            merged[-1] = Segment(merged[-1].text + seg.text, seg.highlight)
        else:
            merged.append(seg)
    return tuple(merged)


def query_terms(query: str) -> set[str]:
    """Positive words of a websearch-style query, lower-cased; codes split like the index."""
    terms: set[str] = set()
    for token in _QUOTED_OR_WORD.findall(query):
        if token.startswith("-") and len(token) > 1:
            continue  # exclusion: never highlight what the user asked NOT to see
        terms.update(w.casefold() for w in _WORD.findall(split_codes(token)))
    return terms - STOP_TERMS


def highlight_terms(text: str, terms: set[str]) -> tuple[Segment, ...]:
    """Mark whole words of `text` that are query terms (case-insensitive)."""
    if not terms:
        return (Segment(text),) if text else ()
    segments: list[Segment] = []
    last = 0
    for match in _WORD.finditer(text):
        if match.group().casefold() in terms:
            if match.start() > last:
                segments.append(Segment(text[last : match.start()]))
            segments.append(Segment(match.group(), True))
            last = match.end()
    if last < len(text):
        segments.append(Segment(text[last:]))
    return _merge(segments)


def best_window(content: str, query: str, *, words: int = WINDOW_WORDS, stride: int = WINDOW_STRIDE) -> str:
    """The window of `words` words sharing the most query terms (first one on ties)."""
    tokens = content.split()
    if len(tokens) <= words:
        return " ".join(tokens)
    terms = query_terms(query)
    best_start, best_score = 0, -1
    for start in range(0, len(tokens) - words + stride, stride):
        window = tokens[start : start + words]
        score = sum(1 for t in window for w in _WORD.findall(split_codes(t)) if w.casefold() in terms)
        if score > best_score:
            best_start, best_score = start, score
    window_text = " ".join(tokens[best_start : best_start + words])
    prefix = "… " if best_start > 0 else ""
    suffix = " …" if best_start + words < len(tokens) else ""
    return f"{prefix}{window_text}{suffix}"


def keyword_snippet(headline: str | None, content: str, query: str) -> tuple[Segment, ...]:
    """Prefer ts_headline's marks; if it marked nothing (e.g. a part of a code that
    the 'simple' parser glued to a sign), highlight query words in Python."""
    if headline and START in headline:
        return from_headline(" ".join(headline.split()))
    text = " ".join(strip_sentinels(headline).split()) if headline else best_window(content, query)
    return highlight_terms(text, query_terms(query))


def semantic_snippet(content: str, query: str) -> tuple[Segment, ...]:
    return highlight_terms(best_window(strip_sentinels(content), query), query_terms(query))


def field_snippet(label: str, value: str, query: str) -> tuple[Segment, ...]:
    return (Segment(f"{label}: "), *highlight_terms(value, query_terms(query)))
