"""Invoice data model and the seeded factory.

Ground truth follows one rule: a field's expected value is what a careful human
could determine from the document itself. A field the document does not state
(no "Bill To" line, no tax line, a bare "$" with no currency code) is expected
to be null, so a model that fills it in is hallucinating.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal

CENT: Final = Decimal("0.01")

LAYOUTS: Final = (
    "classic",
    "modern",
    "minimal",
    "table_heavy",
    "european",
    "tax_inclusive",
    "receipt",
    "credit_note",
)
Layout = Literal[
    "classic", "modern", "minimal", "table_heavy", "european", "tax_inclusive", "receipt", "credit_note"
]
DateStyle = Literal["iso", "long", "dmy_dots", "slash_ambiguous"]
CurrencyStyle = Literal["code", "symbol_code", "symbol_only"]

SUPPLIERS: Final = (
    "Brightwell Medical Supplies Ltd",
    "Northgate Office Partners",
    "Halden & Rook Consulting",
    "Kestrel Lab Services GmbH",
    "Bluefin Cleaning Co.",
    "Orchard Lane Catering",
    "Pinecrest Software Ltd",
    "Silverbay Logistics BV",
    "Tamsin Ward Printing",
    "Copperfield Electrical Ltd",
)
CUSTOMERS: Final = (
    "Riverside Care Home",
    "Elmstead Community Clinic",
    "Harbourview Dental Practice",
    "St. Aldric Hospice",
    "Westmoor Physio Centre",
    "Larkspur Family Surgery",
)
ITEMS: Final = (
    ("Nitrile gloves (box of 100)", "8.40", "14.90"),
    ("Surgical face masks (50)", "6.00", "11.50"),
    ("Hand sanitiser 5L", "18.00", "32.00"),
    ("Printer paper A4 (5 reams)", "19.99", "27.50"),
    ("Consulting hours", "65.00", "140.00"),
    ("Deep clean, ward B", "180.00", "420.00"),
    ("Lunch platter for 12", "54.00", "96.00"),
    ("Software licence (annual)", "240.00", "890.00"),
    ("Courier delivery", "7.50", "19.00"),
    ("Cable tray installation", "85.00", "260.00"),
    ("Thermometer probe covers", "3.20", "6.80"),
    ("Disposable aprons (200)", "9.90", "17.40"),
)
CURRENCIES: Final = ("EUR", "GBP", "USD")
SYMBOLS: Final = {"EUR": "€", "GBP": "£", "USD": "$"}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal

    def truth(self) -> dict[str, str]:
        return {
            "description": self.description,
            "quantity": f"{self.quantity.normalize():f}",
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
        }


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_number: str | None
    invoice_date: date
    supplier_name: str
    customer_name: str | None
    currency: str
    line_items: tuple[LineItem, ...]
    subtotal: Decimal
    tax: Decimal | None
    total: Decimal
    tax_rate: Decimal | None
    layout: Layout = "classic"
    document_type: Literal["invoice", "receipt", "credit_note"] = "invoice"
    date_style: DateStyle = "iso"
    currency_style: CurrencyStyle = "code"
    # Planted errors print a value that differs from the arithmetic truth.
    printed_total: Decimal | None = None
    printed_subtotal: Decimal | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def shown_total(self) -> Decimal:
        return self.printed_total if self.printed_total is not None else self.total

    @property
    def shown_subtotal(self) -> Decimal:
        return self.printed_subtotal if self.printed_subtotal is not None else self.subtotal

    @property
    def european(self) -> bool:
        return self.layout == "european"

    def truth(self) -> dict[str, Any]:
        """Expected extraction: the values PRINTED on the document (the system
        must report what the page says; validation flags the inconsistency)."""
        return {
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat(),
            "supplier_name": self.supplier_name,
            "customer_name": self.customer_name,
            "subtotal": None if self.layout == "minimal" else str(self.shown_subtotal),
            "tax": None if self.tax is None else str(self.tax),
            "total": str(self.shown_total),
            # A bare "$" could be USD, CAD, AUD...: not determinable. € and £ are.
            "currency": None if self.currency_style == "symbol_only" and self.currency == "USD" else self.currency,
            "line_items": [item.truth() for item in self.line_items],
        }

    def with_(self, **changes: Any) -> Invoice:
        return replace(self, **changes)


class InvoiceFactory:
    """Deterministic: the same seed and call sequence give the same invoices."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self._numbers: set[str] = set()

    @property
    def rng(self) -> random.Random:
        return self._rng

    def invoice_number(self, prefix: str = "EV") -> str:
        rng = self._rng
        while True:
            style = rng.choice(("dash", "slash", "plain", "letter"))
            n = rng.randint(1, 9999)
            number = {
                "dash": f"{prefix}-2026-{n:04d}",
                "slash": f"{prefix}/26/{n:04d}",
                "plain": f"{rng.randint(10, 99)}{n:05d}",
                "letter": f"{prefix[0]}{rng.choice('ABCDEFGH')}-{n:05d}",
            }[style]
            if number not in self._numbers:
                self._numbers.add(number)
                return number

    def line_items(self, count: int, *, sign: int = 1) -> tuple[LineItem, ...]:
        rng = self._rng
        picks = [ITEMS[rng.randrange(len(ITEMS))] for _ in range(count)]
        items = []
        for description, low, high in picks:
            unit = money(Decimal(str(rng.uniform(float(low), float(high)))))
            qty = Decimal(rng.choice((1, 1, 2, 3, 4, 5, 6, 10, 12, 20)))
            items.append(LineItem(description, qty, sign * unit, money(sign * qty * unit)))
        return tuple(items)

    def invoice(
        self,
        *,
        layout: Layout = "classic",
        n_items: int | None = None,
        missing_customer: bool | None = None,
        no_tax: bool | None = None,
        date_style: DateStyle | None = None,
        currency_style: CurrencyStyle | None = None,
        invoice_date: date | None = None,
    ) -> Invoice:
        rng = self._rng
        document_type: Literal["invoice", "receipt", "credit_note"] = (
            "receipt" if layout == "receipt" else "credit_note" if layout == "credit_note" else "invoice"
        )
        sign = -1 if document_type == "credit_note" else 1
        items = self.line_items(n_items if n_items is not None else rng.randint(1, 4), sign=sign)
        lines_sum = money(sum((i.amount for i in items), Decimal(0)))
        if no_tax is None:
            no_tax = layout != "tax_inclusive" and rng.random() < 0.15
        rate = None if no_tax else Decimal(rng.choice(("0.20", "0.19", "0.10", "0.05")))
        if layout == "tax_inclusive" and rate is not None:
            # Line amounts are gross; the page states the VAT contained in the total.
            total = lines_sum
            subtotal = money(total / (1 + rate))
            tax: Decimal | None = total - subtotal
        else:
            subtotal = lines_sum
            tax = None if rate is None else money(subtotal * rate)
            total = subtotal + (tax or Decimal(0))
        if missing_customer is None:
            missing_customer = document_type == "receipt" or rng.random() < 0.15
        currency = "EUR" if layout == "european" else rng.choice(CURRENCIES)
        if date_style is None:
            date_style = (
                "dmy_dots" if layout == "european" else rng.choice(("iso", "iso", "long", "dmy_dots"))
            )
        if currency_style is None:
            currency_style = rng.choice(("code", "code", "symbol_code"))
        day = invoice_date or date(2025, 6, 1) + timedelta(days=rng.randint(0, 300))
        return Invoice(
            invoice_number=self.invoice_number("CN" if document_type == "credit_note" else "EV"),
            invoice_date=day,
            supplier_name=rng.choice(SUPPLIERS),
            customer_name=None if missing_customer else rng.choice(CUSTOMERS),
            currency=currency,
            line_items=items,
            subtotal=subtotal,
            tax=tax,
            total=total,
            tax_rate=rate,
            layout=layout,
            document_type=document_type,
            date_style=date_style,
            currency_style=currency_style,
        )
