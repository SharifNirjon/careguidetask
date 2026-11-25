import { describe, expect, it } from "vitest";
import { at } from "@/test/at";

import { extraction } from "@/test/factories";

import { buildCorrection, formPathFor, lineHint, reviewFormSchema, sumHint, toFormValues } from "./schemas";

const base = toFormValues(extraction());

describe("buildCorrection (PATCH sends only what changed)", () => {
  it("returns null when nothing changed", () => {
    expect(buildCorrection(base, structuredClone(base))).toBeNull();
  });

  it("ignores numerically equal amounts", () => {
    const current = structuredClone(base);
    current.fields.total = "300";
    expect(buildCorrection(base, current)).toBeNull();
  });

  it("sends only dirty header fields; empty means null", () => {
    const current = structuredClone(base);
    current.fields.total = "270.00";
    current.fields.customer_name = "";
    current.reason = "  OCR misread  ";
    expect(buildCorrection(base, current)?.request).toEqual({
      fields: { total: "270.00", customer_name: null },
      reason: "OCR misread",
    });
  });

  it("turns line edits into add / update / delete ops and maps them to rows", () => {
    const current = structuredClone(base);
    current.lineItems.push({
      id: null,
      position: null,
      description: "Freight",
      quantity: "",
      unit_price: "",
      amount: "10",
      tax_rate: "",
    });
    current.lineItems[0] = { ...at(current.lineItems, 0), amount: "230" };
    const draft = buildCorrection(base, current);
    expect(draft?.request.line_items).toEqual([
      { op: "update", id: "44444444-4444-4444-8444-444444444444", amount: "230" },
      { op: "add", description: "Freight", amount: "10" },
    ]);
    expect(draft?.opRows).toEqual([0, 1]);

    const removed = { ...structuredClone(base), lineItems: [] };
    expect(buildCorrection(base, removed)).toEqual({
      request: { line_items: [{ op: "delete", id: "44444444-4444-4444-8444-444444444444" }] },
      opRows: [-1],
    });
  });
});

describe("form schema mirrors the backend formats", () => {
  it.each([
    ["total", "12,50", false],
    ["total", "12.5", true],
    ["total", "", true],
    ["invoice_date", "2026-02-30", false],
    ["invoice_date", "01/03/2026", false],
    ["invoice_date", "2026-03-01", true],
    ["currency", "eur", true],
    ["currency", "EURO", false],
  ] as const)("%s = %s valid: %s", (name, value, ok) => {
    const values = structuredClone(base);
    values.fields[name] = value;
    expect(reviewFormSchema.safeParse(values).success).toBe(ok);
  });
});

describe("hints (UX only)", () => {
  it("flags quantity x price != amount, exactly", () => {
    const row = { ...at(base.lineItems, 0), quantity: "3", unit_price: "0.1", amount: "0.30" };
    expect(lineHint(row)).toBeNull(); // 3 x 0.1 is exactly 0.30 here (no float 0.30000000000000004)
    expect(lineHint({ ...row, amount: "0.40" })).toBe("3 × 0.1 = 0.30, not 0.40");
  });

  it("flags lines that don't add up to the subtotal", () => {
    const values = structuredClone(base);
    values.fields.subtotal = "200";
    expect(sumHint(values)).toBe("Lines add up to 225.00, subtotal is 200");
    expect(sumHint(base)).toBeNull();
  });
});

describe("formPathFor", () => {
  it("maps issue field paths to form fields", () => {
    expect(formPathFor("total", base)).toBe("fields.total");
    expect(formPathFor("line_items[0].amount", base)).toBe("lineItems.0.amount");
    expect(formPathFor("line_items[9].amount", base)).toBeNull();
    expect(formPathFor(null, base)).toBeNull();
  });
});
