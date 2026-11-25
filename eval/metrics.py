"""Pure evaluation metrics. No I/O: everything here is unit-tested on tiny cases.

Definitions (also rendered into docs/EVALUATION.md, section 3):

Field outcome, per (expected invoice, field), after normalization:
  correct       expected non-null, predicted equal
  wrong         expected non-null, predicted non-null but different
  missing       expected non-null, predicted null
  hallucinated  expected null, predicted non-null
  true_null     both null
Per field:
  accuracy          = (correct + true_null) / all slots
  precision         = correct / (correct + wrong + hallucinated)      (when we output a value)
  recall            = correct / (correct + wrong + missing)           (of values present in ground truth)
  missing rate      = missing / (correct + wrong + missing)
  hallucination rate= hallucinated / (hallucinated + true_null)       (of fields the document does not state)
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from rapidfuzz import fuzz

FIELDS: Final = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "customer_name",
    "subtotal",
    "tax",
    "total",
    "currency",
)
AMOUNT_FIELDS: Final = frozenset({"subtotal", "tax", "total"})
NAME_FIELDS: Final = frozenset({"supplier_name", "customer_name"})
# "All required fields correct": the fields whose absence is an error for an invoice.
REQUIRED_FIELDS: Final = ("invoice_number", "invoice_date", "supplier_name", "total", "currency")
LENIENT_NAME_MIN: Final = 90.0  # rapidfuzz ratio, 0-100
LINE_DESC_MATCH_MIN: Final = 80.0

Outcome = Literal["correct", "wrong", "missing", "hallucinated", "true_null"]


# ------------------------------------------------------------ normalization


def norm_amount(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def norm_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split()) or None


def norm_code(value: str | None) -> str | None:
    """Invoice numbers: case, spaces and separators ignored ('EV-26/7' == 'ev 26 7')."""
    if value is None:
        return None
    return re.sub(r"[^0-9a-z]", "", value.casefold()) or None


def normalize(name: str, value: str | None) -> object:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if name in AMOUNT_FIELDS:
        return norm_amount(value)
    if name == "invoice_number":
        return norm_code(value)
    if name == "currency":
        return value.strip().upper()
    if name == "invoice_date":
        return value.strip()[:10]
    return norm_text(value)


def name_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm_text(a), norm_text(b)
    if na is None or nb is None:
        return 0.0
    return float(fuzz.ratio(na, nb))


def field_outcome(name: str, expected: str | None, predicted: str | None, *, lenient: bool = False) -> Outcome:
    exp, pred = normalize(name, expected), normalize(name, predicted)
    if exp is None:
        return "true_null" if pred is None else "hallucinated"
    if pred is None:
        return "missing"
    if exp == pred:
        return "correct"
    if lenient and name in NAME_FIELDS and name_similarity(expected, predicted) >= LENIENT_NAME_MIN:
        return "correct"
    return "wrong"


# --------------------------------------------------------------- counting


@dataclass
class Counts:
    correct: int = 0
    wrong: int = 0
    missing: int = 0
    hallucinated: int = 0
    true_null: int = 0

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def merge(self, other: Counts) -> None:
        for key in ("correct", "wrong", "missing", "hallucinated", "true_null"):
            setattr(self, key, getattr(self, key) + getattr(other, key))

    @property
    def slots(self) -> int:
        return self.correct + self.wrong + self.missing + self.hallucinated + self.true_null

    @property
    def present(self) -> int:
        return self.correct + self.wrong + self.missing

    @property
    def predicted(self) -> int:
        return self.correct + self.wrong + self.hallucinated

    @property
    def absent(self) -> int:
        return self.hallucinated + self.true_null

    def rates(self) -> dict[str, Rate]:
        return {
            "accuracy": Rate(self.correct + self.true_null, self.slots),
            "precision": Rate(self.correct, self.predicted),
            "recall": Rate(self.correct, self.present),
            "missing_rate": Rate(self.missing, self.present),
            "hallucination_rate": Rate(self.hallucinated, self.absent),
        }


# ------------------------------------------------------------- statistics


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """95 % Wilson score interval. (0, 1) when n == 0 (no information)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return None if self.n == 0 else self.k / self.n

    def as_dict(self) -> dict[str, Any]:
        low, high = wilson(self.k, self.n)
        return {
            "k": self.k,
            "n": self.n,
            "value": self.value,
            "ci95": [low, high] if self.n else None,
            "small_n": self.n < 5,
        }


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def distribution(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
        "mean": sum(values) / len(values) if values else None,
    }


# --------------------------------------------------------------- matching


def hungarian(cost: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Minimum-cost assignment for a rectangular matrix (rows x cols).

    Returns min(rows, cols) (row, col) pairs. O(n^3) Kuhn-Munkres with
    potentials (e-maxx formulation), padded to a square matrix.
    """
    rows = len(cost)
    cols = len(cost[0]) if rows else 0
    if rows == 0 or cols == 0:
        return []
    n = max(rows, cols)
    big = max((abs(c) for row in cost for c in row), default=0.0) + 1.0
    a = [[(cost[i][j] if i < rows and j < cols else big) for j in range(n)] for i in range(n)]
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    pairs = [(p[j] - 1, j - 1) for j in range(1, n + 1) if p[j] and p[j] - 1 < rows and j - 1 < cols]
    return sorted(pairs)


def invoice_similarity(expected: Mapping[str, Any], predicted: Mapping[str, Any]) -> int:
    """Number of non-null fields that agree (used when invoice numbers can't pair them)."""
    score = 0
    for name in ("invoice_date", "supplier_name", "customer_name", "total", "subtotal", "tax"):
        exp = normalize(name, expected.get(name))
        if exp is not None and exp == normalize(name, predicted.get(name)):
            score += 1
    return score


@dataclass
class InvoiceMatch:
    pairs: list[tuple[int, int]]  # (expected index, predicted index)
    missed: list[int]  # expected invoices with no prediction
    spurious: list[int]  # predicted invoices with no ground truth


def match_invoices(
    expected: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]], *, min_similarity: int = 2
) -> InvoiceMatch:
    """Pair by normalized invoice number first, then optimal assignment on field agreement."""
    pairs: list[tuple[int, int]] = []
    used_e: set[int] = set()
    used_p: set[int] = set()
    by_number: dict[str, list[int]] = {}
    for j, pred in enumerate(predicted):
        key = norm_code(pred.get("invoice_number"))
        if key:
            by_number.setdefault(key, []).append(j)
    for i, exp in enumerate(expected):
        key = norm_code(exp.get("invoice_number"))
        candidates = [j for j in by_number.get(key or "", []) if j not in used_p]
        if key and candidates:
            pairs.append((i, candidates[0]))
            used_e.add(i)
            used_p.add(candidates[0])
    rest_e = [i for i in range(len(expected)) if i not in used_e]
    rest_p = [j for j in range(len(predicted)) if j not in used_p]
    if rest_e and rest_p:
        sims = [[invoice_similarity(expected[i], predicted[j]) for j in rest_p] for i in rest_e]
        for r, c in hungarian([[-s for s in row] for row in sims]):
            if sims[r][c] >= min_similarity:
                pairs.append((rest_e[r], rest_p[c]))
                used_e.add(rest_e[r])
                used_p.add(rest_p[c])
    return InvoiceMatch(
        pairs=sorted(pairs),
        missed=[i for i in range(len(expected)) if i not in used_e],
        spurious=[j for j in range(len(predicted)) if j not in used_p],
    )


# ------------------------------------------------------------- line items


@dataclass
class LineScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    attr: dict[str, list[int]] = field(default_factory=lambda: {"quantity": [0, 0], "unit_price": [0, 0], "amount": [0, 0]})

    def merge(self, other: LineScore) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        for key, (ok, n) in other.attr.items():
            self.attr[key][0] += ok
            self.attr[key][1] += n

    def as_dict(self) -> dict[str, Any]:
        precision = Rate(self.tp, self.tp + self.fp)
        recall = Rate(self.tp, self.tp + self.fn)
        p, r = precision.value, recall.value
        f1 = None if not p or not r else 2 * p * r / (p + r)
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": precision.as_dict(),
            "recall": recall.as_dict(),
            "f1": f1 if (p is not None and r is not None) else None,
            "attribute_accuracy": {k: Rate(ok, n).as_dict() for k, (ok, n) in self.attr.items()},
        }


def _line_pair_ok(exp: Mapping[str, Any], pred: Mapping[str, Any]) -> bool:
    desc = name_similarity(exp.get("description"), pred.get("description"))
    exp_amount, pred_amount = norm_amount(exp.get("amount")), norm_amount(pred.get("amount"))
    same_amount = exp_amount is not None and exp_amount == pred_amount
    return desc >= LINE_DESC_MATCH_MIN or (same_amount and desc >= 50.0)


def score_lines(expected: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]]) -> LineScore:
    """Optimal assignment on description similarity + amount, then per-attribute accuracy."""
    score = LineScore()
    if expected and predicted:
        cost = [
            [
                -(name_similarity(e.get("description"), p.get("description")) / 100
                  + (1.0 if norm_amount(e.get("amount")) == norm_amount(p.get("amount")) else 0.0))
                for p in predicted
            ]
            for e in expected
        ]  # fmt: skip
        matched = [(i, j) for i, j in hungarian(cost) if _line_pair_ok(expected[i], predicted[j])]
    else:
        matched = []
    score.tp = len(matched)
    score.fn = len(expected) - len(matched)
    score.fp = len(predicted) - len(matched)
    for i, j in matched:
        for key in ("quantity", "unit_price", "amount"):
            ok = norm_amount(expected[i].get(key)) == norm_amount(predicted[j].get(key))
            score.attr[key][0] += int(ok)
            score.attr[key][1] += 1
    return score


# ------------------------------------------------------------ calibration


def calibration(pairs: Iterable[tuple[float, bool]], bins: int = 10) -> dict[str, Any]:
    """Reliability table and expected calibration error over (confidence, correct)."""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    total = 0
    for conf, ok in pairs:
        index = min(bins - 1, max(0, int(conf * bins)))
        buckets[index].append((conf, ok))
        total += 1
    table = []
    ece = 0.0
    for b, items in enumerate(buckets):
        if not items:
            continue
        acc = sum(ok for _, ok in items) / len(items)
        mean_conf = sum(c for c, _ in items) / len(items)
        ece += len(items) / total * abs(acc - mean_conf)
        table.append(
            {"bin": [b / bins, (b + 1) / bins], "n": len(items), "accuracy": acc, "mean_confidence": mean_conf}
        )
    return {"n": total, "ece": ece if total else None, "table": table}


def automation_tradeoff(
    invoices: Sequence[tuple[float | None, bool, bool]], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    """invoices: (overall confidence, has open error issues, fully correct).

    An invoice is auto-approved at threshold t when it has no open errors and
    confidence >= t (the Step 5 policy, without blocking warnings)."""
    rows = []
    for t in thresholds:
        auto = [ok for conf, errors, ok in invoices if not errors and conf is not None and conf >= t]
        rows.append(
            {
                "threshold": t,
                "auto_approved": Rate(len(auto), len(invoices)).as_dict(),
                "correct_among_auto": Rate(sum(auto), len(auto)).as_dict(),
            }
        )
    return rows


# --------------------------------------------------------------- reliability

TERMINAL_OK: Final = frozenset({"completed", "needs_review"})


def reliability(docs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """docs: {valid: bool, status: str | None, error_code: str | None, expected_error_code: str | None}.

    success rate  = valid docs that reached completed or needs_review / valid docs
    failure rate  = valid docs that failed or never reached a terminal state / valid docs
    expected-failure handling = invalid files rejected with the expected code / invalid files
    """
    valid = [d for d in docs if d["valid"]]
    invalid = [d for d in docs if not d["valid"]]
    ok = sum(d["status"] in TERMINAL_OK for d in valid)
    handled = sum(d["status"] in ("failed", "rejected") and d["error_code"] == d["expected_error_code"] for d in invalid)
    return {
        "success_rate": Rate(ok, len(valid)).as_dict(),
        "failure_rate": Rate(len(valid) - ok, len(valid)).as_dict(),
        "expected_failure_handling": Rate(handled, len(invalid)).as_dict(),
    }


# ------------------------------------------------------------------ search


def recall_at(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    rel = set(relevant)
    return len(rel & set(ranked[:k])) / len(rel) if rel else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str]) -> float:
    rel = set(relevant)
    for position, item in enumerate(ranked, 1):
        if item in rel:
            return 1 / position
    return 0.0


# ------------------------------------------------------------- validation


def rule_scores(
    files: Sequence[tuple[set[str], set[str]]], rules: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """files: (expected rule codes, raised rule codes). Per rule: file-level precision/recall."""
    out = {}
    for rule in rules:
        tp = sum(rule in exp and rule in got for exp, got in files)
        fp = sum(rule not in exp and rule in got for exp, got in files)
        fn = sum(rule in exp and rule not in got for exp, got in files)
        out[rule] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": Rate(tp, tp + fp).as_dict(),
            "recall": Rate(tp, tp + fn).as_dict(),
        }
    return out


def group_by(items: Iterable[Any], key: Callable[[Any], str]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for item in items:
        out.setdefault(key(item), []).append(item)
    return out
