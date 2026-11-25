"""Seeded synthetic invoice generator with ground truth.

    from tools.synth import InvoiceFactory, render
    inv = InvoiceFactory(seed=7).invoice(layout="classic")
    pdf_bytes = render.digital_pdf([render.layout_lines(inv)])
    truth = inv.truth()          # what a careful reader can determine from the page

Everything is deterministic for a given seed. All names are invented; nothing
is copied from real documents (free LLM endpoints may log traffic).
"""

from tools.synth.model import LAYOUTS, Invoice, InvoiceFactory, LineItem

__all__ = ["LAYOUTS", "Invoice", "InvoiceFactory", "LineItem"]
