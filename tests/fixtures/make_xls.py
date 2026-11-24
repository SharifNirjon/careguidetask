"""One-off generator for the committed tests/fixtures/invoice.xls (BIFF8).

Requires `pip install xlwt` (not a project dependency: xlwt only writes .xls,
and only this script needs it). Re-run only if the fixture must change:

    python tests/fixtures/make_xls.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlwt

OUT = Path(__file__).with_name("invoice.xls")


def main() -> None:
    book = xlwt.Workbook()
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")

    ws = book.add_sheet("Invoices")
    ws.write_merge(0, 0, 0, 3, "Register March 2024")  # merged title row
    for col, name in enumerate(["Invoice No", "Date", "Customer", "Amount"]):
        ws.write(2, col, name)
    rows = [
        ("00123", dt.date(2024, 3, 5), "Northwind Clinic", "1.234,56"),
        ("00124", dt.date(2024, 3, 9), "Northwind Clinic", 99.0),
    ]
    for r, (num, date, customer, amount) in enumerate(rows, start=3):
        ws.write(r, 0, num)
        ws.write(r, 1, date, date_style)
        ws.write(r, 2, customer)
        ws.write(r, 3, amount)

    hidden = book.add_sheet("Rates")
    hidden.write(0, 0, "Code")
    hidden.write(0, 1, "Rate")
    hidden.write(1, 0, "EUR")
    hidden.write(1, 1, 1.0)
    hidden.visibility = 1  # hidden

    book.save(str(OUT))


if __name__ == "__main__":
    main()
