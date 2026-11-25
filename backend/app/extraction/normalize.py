"""Raw strings -> typed values. Pure and deterministic; the LLM never does this.

Every parser returns Parsed(value, ambiguous, note). `ambiguous` means a
value was chosen but the document didn't fully settle it (the caller lowers
confidence and records the note); value None means "refuse to guess".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Generic, TypeVar

T = TypeVar("T")

AMOUNT_QUANT: Final = Decimal("0.0001")
MAX_AMOUNT: Final = Decimal("99999999999999.9999")  # NUMERIC(18, 4)
THOUSANDS_GROUP: Final = 3
TWO_DIGIT_YEAR_PIVOT: Final = 70  # 00-69 -> 20xx, 70-99 -> 19xx
MAX_MONTH: Final = 12


@dataclass(frozen=True, slots=True)
class Parsed(Generic[T]):
    value: T | None
    ambiguous: bool = False
    note: str | None = None


# ------------------------------------------------------------------ amounts

_MINUS_SIGNS: Final = "-\u2212\u2013"
_GROUPING_CHARS: Final = re.compile(r"[\s'\u00a0\u202f\u2009]")
_NOT_NUMERIC: Final = re.compile(r"[^\d.,()\-\u2212\u2013]")


def _is_negative(text: str) -> bool:
    stripped = text.strip()
    return (
        (stripped.startswith("(") and stripped.endswith(")"))
        or stripped[:1] in _MINUS_SIGNS
        or stripped[-1:] in _MINUS_SIGNS
    )


def _single_separator_role(digits: str, sep: str, hint: str | None) -> tuple[str, bool]:
    """'decimal' or 'thousands' for a number with exactly one kind of separator."""
    if digits.count(sep) > 1:
        return "thousands", False
    head, tail = digits.split(sep)
    if len(tail) != THOUSANDS_GROUP or not head or len(head) > THOUSANDS_GROUP:
        return "decimal", False
    if hint is not None:  # "1,234" with document-level evidence
        return ("decimal" if hint == sep else "thousands"), False
    # Truly ambiguous: follow the commonest convention, but flag it.
    return ("thousands" if sep == "," else "decimal"), True


def parse_amount(raw: str | None, decimal_separator: str | None = None) -> Parsed[Decimal]:
    """'1,234.56', '1.234,56', '1 234,56', '(123.45)', '123.45-', '€1,234', 'USD 99' ...

    `decimal_separator` is document-level evidence (see infer_decimal_separator).
    """
    if raw is None or not raw.strip():
        return Parsed(None)
    text = unicodedata.normalize("NFKC", raw)
    negative = _is_negative(_NOT_NUMERIC.sub("", text))
    digits = _GROUPING_CHARS.sub("", _NOT_NUMERIC.sub("", text))
    digits = digits.strip("()" + _MINUS_SIGNS)
    if not digits or not re.fullmatch(r"[\d.,]+", digits) or not re.search(r"\d", digits):
        return Parsed(None, note=f"not an amount: {raw!r}")

    ambiguous = False
    if "," in digits and "." in digits:
        decimal_sep = "," if digits.rfind(",") > digits.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        normalized = digits.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in digits or "." in digits:
        sep = "," if "," in digits else "."
        role, ambiguous = _single_separator_role(digits, sep, decimal_separator)
        normalized = digits.replace(sep, "") if role == "thousands" else digits.replace(sep, ".")
    else:
        normalized = digits
    if normalized.count(".") > 1:
        return Parsed(None, note=f"not an amount: {raw!r}")
    try:
        value = Decimal(normalized).quantize(AMOUNT_QUANT)
    except InvalidOperation:
        return Parsed(None, note=f"not an amount: {raw!r}")
    if value > MAX_AMOUNT:
        return Parsed(None, note=f"amount out of range: {raw!r}")
    note = f"ambiguous separator in {raw!r}; read as {value}" if ambiguous else None
    return Parsed(-value if negative else value, ambiguous, note)


def infer_decimal_separator(raws: Iterable[str | None]) -> str | None:
    """The document's decimal separator, from amounts that are unambiguous on their own."""
    votes: set[str] = set()
    for raw in raws:
        if not raw:
            continue
        digits = _GROUPING_CHARS.sub("", _NOT_NUMERIC.sub("", raw)).strip("()" + _MINUS_SIGNS)
        if "," in digits and "." in digits:
            votes.add("," if digits.rfind(",") > digits.rfind(".") else ".")
        elif digits.count(",") == 1 or digits.count(".") == 1:
            sep = "," if "," in digits else "."
            tail = digits.split(sep)[1]
            if len(tail) != THOUSANDS_GROUP:
                votes.add(sep)
        elif digits.count(",") > 1:
            votes.add(".")
        elif digits.count(".") > 1:
            votes.add(",")
    return votes.pop() if len(votes) == 1 else None


# -------------------------------------------------------------------- dates

MONTHS: Final = {
    name: index
    for index, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_ISO_DATE: Final = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_NUMERIC_DATE: Final = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$")
_DAY_MONTH_NAME: Final = re.compile(r"^(\d{1,2})[\s\-/]+([a-z]+)[\s\-/,]+(\d{2}|\d{4})$")
_MONTH_NAME_DAY: Final = re.compile(r"^([a-z]+)[\s\-/]+(\d{1,2})[\s,]+(\d{4})$")
_ORDINAL: Final = re.compile(r"(\d)(st|nd|rd|th)\b")


def _clean_date(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).strip().lower()
    text = _ORDINAL.sub(r"\1", text).replace(".", " ") if re.search(r"[a-z]", text) else text
    return re.sub(r"\s+", " ", text.replace(",", ", ")).strip(" ,")


def _year(text: str) -> int:
    value = int(text)
    if len(text) == 2:
        return 2000 + value if value < TWO_DIGIT_YEAR_PIVOT else 1900 + value
    return value


def _build(year: int, month: int, day: int, raw: str) -> Parsed[date]:
    try:
        return Parsed(date(year, month, day))
    except ValueError:
        return Parsed(None, note=f"impossible date: {raw!r}")


def parse_date(raw: str | None, *, day_first: bool | None = None) -> Parsed[date]:
    """ISO, numeric (DD/MM vs MM/DD settled by `day_first` or the values), and month names.

    A numeric date that could be either order with no document evidence is
    refused (None + note): an invoice date off by months is worse than none.
    """
    if raw is None or not raw.strip():
        return Parsed(None)
    text = _clean_date(raw)
    if m := _ISO_DATE.match(text):
        return _build(int(m[1]), int(m[2]), int(m[3]), raw)
    if m := _NUMERIC_DATE.match(text):
        first, second, year = int(m[1]), int(m[2]), _year(m[3])
        if first > MAX_MONTH:
            return _build(year, second, first, raw)
        if second > MAX_MONTH:
            return _build(year, first, second, raw)
        if first == second:
            return _build(year, first, first, raw)
        if day_first is None:
            return Parsed(None, ambiguous=True, note=f"ambiguous day/month order: {raw!r}")
        return _build(year, second, first, raw) if day_first else _build(year, first, second, raw)
    if (m := _DAY_MONTH_NAME.match(text)) and m[2] in MONTHS:
        return _build(_year(m[3]), MONTHS[m[2]], int(m[1]), raw)
    if (m := _MONTH_NAME_DAY.match(text)) and m[1] in MONTHS:
        return _build(int(m[3]), MONTHS[m[1]], int(m[2]), raw)
    return Parsed(None, note=f"unrecognized date: {raw!r}")


def infer_day_first(raws: Iterable[str | None]) -> bool | None:
    """Document-level DD/MM vs MM/DD evidence: a component > 12, or a month-name date's order."""
    votes: set[bool] = set()
    for raw in raws:
        if not raw:
            continue
        text = _clean_date(raw)
        if m := _NUMERIC_DATE.match(text):
            if int(m[1]) > MAX_MONTH:
                votes.add(True)
            elif int(m[2]) > MAX_MONTH:
                votes.add(False)
        elif (m := _DAY_MONTH_NAME.match(text)) and m[2] in MONTHS:
            votes.add(True)
        elif (m := _MONTH_NAME_DAY.match(text)) and m[1] in MONTHS:
            votes.add(False)
    return votes.pop() if len(votes) == 1 else None


_DATE_IN_TEXT: Final = re.compile(
    r"\b\d{1,2}[-/.]\d{1,2}[-/.](?:\d{4}|\d{2})\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?[\s\-]+[A-Za-z]{3,9}\.?[\s\-,]+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
)
_AMOUNT_IN_TEXT: Final = re.compile(r"(?<![\w.,])\d{1,3}(?:[.,' \u00a0]\d{3})*[.,]\d{1,2}(?![\d.,])")


def find_date_strings(text: str) -> list[str]:
    """Date-like strings anywhere in the document: evidence for day/month order."""
    return _DATE_IN_TEXT.findall(text)


def find_amount_strings(text: str) -> list[str]:
    """Amount-like strings anywhere in the document: evidence for the decimal separator."""
    return _AMOUNT_IN_TEXT.findall(text)


# ----------------------------------------------------------------- currency

ISO_4217: Final = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "ANG",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XCD",
        "XOF",
        "XPF",
        "YER",
        "ZAR",
        "ZMW",
        "ZWL",
    ]
)
PREFIXED_SYMBOLS: Final = {  # longest first matters: "US$" before "$"
    "US$": "USD",
    "AU$": "AUD",
    "A$": "AUD",
    "CA$": "CAD",
    "C$": "CAD",
    "NZ$": "NZD",
    "S$": "SGD",
    "HK$": "HKD",
    "R$": "BRL",
    "MX$": "MXN",
    "CN¥": "CNY",
    "JP¥": "JPY",
    "RMB": "CNY",
}
UNAMBIGUOUS_SYMBOLS: Final = {
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "৳": "BDT",
    "₩": "KRW",
    "₦": "NGN",
    "₱": "PHP",
    "₫": "VND",
    "₪": "ILS",
    "₺": "TRY",
    "₴": "UAH",
    "₽": "RUB",
    "฿": "THB",
}
AMBIGUOUS_SYMBOLS: Final = {
    "$": ("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"),
    "¥": ("JPY", "CNY"),
    "kr": ("SEK", "NOK", "DKK", "ISK"),
}
_CODE_TOKEN: Final = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")


def _codes_in(text: str, allowed: Iterable[str] = ISO_4217) -> set[str]:
    allowed_set = set(allowed)
    return {code for code in _CODE_TOKEN.findall(text) if code in allowed_set}


def _symbol_code(text: str) -> tuple[str | None, str | None]:
    """(code, ambiguous symbol) from a symbol-bearing string."""
    upper = text.upper()
    for symbol in sorted(PREFIXED_SYMBOLS, key=len, reverse=True):
        if symbol.upper() in upper:
            return PREFIXED_SYMBOLS[symbol], None
    for symbol, code in UNAMBIGUOUS_SYMBOLS.items():
        if symbol in text:
            return code, None
    for symbol in AMBIGUOUS_SYMBOLS:
        pattern = rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])" if symbol.isalpha() else re.escape(symbol)
        if re.search(pattern, text, re.I):
            return None, symbol
    return None, None


def normalize_currency(
    raw: str | None, *, document_text: str = "", amount_raws: Iterable[str | None] = ()
) -> Parsed[str]:
    """ISO 4217 code. Explicit codes win; unambiguous symbols map; '$', '¥', 'kr'
    resolve only with explicit textual evidence. Never defaults to USD."""
    sources = [raw or "", *(a or "" for a in amount_raws)]
    for source in sources:
        codes = _codes_in(source.upper())
        if len(codes) == 1:
            return Parsed(codes.pop())
    for source in sources:
        code, ambiguous_symbol = _symbol_code(source)
        if code:
            return Parsed(code)
        if ambiguous_symbol:
            candidates = AMBIGUOUS_SYMBOLS[ambiguous_symbol]
            found = _codes_in(document_text.upper(), candidates)
            if len(found) == 1:
                return Parsed(found.pop(), note=f"{ambiguous_symbol!r} resolved from document text")
            return Parsed(
                None,
                ambiguous=True,
                note=f"currency symbol {ambiguous_symbol!r} is ambiguous; no explicit code found",
            )
    return Parsed(None, note="no currency printed" if not raw else f"unrecognized currency: {raw!r}")


# --------------------------------------------------------------- match keys

LEGAL_SUFFIXES: Final = frozenset(
    {
        "ltd",
        "limited",
        "llc",
        "inc",
        "incorporated",
        "gmbh",
        "pvt",
        "private",
        "plc",
        "co",
        "corp",
        "corporation",
        "company",
        "sa",
        "sarl",
        "bv",
        "ag",
        "ab",
        "oy",
        "srl",
        "spa",
        "kg",
        "pty",
        "llp",
        "lp",
        "nv",
        "oü",
        "as",
    }
)
_NON_WORD: Final = re.compile(r"[^\w]+")


def supplier_name_norm(name: str | None) -> str | None:
    """Duplicate-detection key: 'ACME Supplies Ltd.' == 'Acme Supplies, Limited'."""
    if not name:
        return None
    text = _NON_WORD.sub(" ", unicodedata.normalize("NFKC", name).casefold()).replace("_", " ")
    tokens = text.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or None


def invoice_number_norm(number: str | None) -> str | None:
    """'inv-2024/001' == 'INV 2024 001' == 'INV2024001'."""
    if not number:
        return None
    text = unicodedata.normalize("NFKC", number).upper()
    return "".join(ch for ch in text if ch.isalnum()) or None
