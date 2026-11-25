import { z } from "zod";

import type { CorrectionRequest, ExtractionOut, LineItemOp } from "@/lib/api/types";
import { absolute, fromScaled, HINT_TOLERANCE, multiply, toScaled } from "@/lib/format/decimal";

/**
 * The review form mirrors the backend's correction formats: amounts are plain
 * decimals ("1234.56"), dates ISO ("2026-03-01"), currencies ISO 4217. Empty
 * means "clear the value" (sent as null). The server re-validates everything;
 * this only catches typos before a round trip.
 */
export const HEADER_FIELDS = [
  "invoice_number",
  "invoice_date",
  "supplier_name",
  "customer_name",
  "subtotal",
  "tax",
  "total",
  "currency",
] as const;
export type HeaderField = (typeof HEADER_FIELDS)[number];

export const LINE_FIELDS = ["description", "quantity", "unit_price", "amount", "tax_rate"] as const;
export type LineField = (typeof LINE_FIELDS)[number];

export const FIELD_LABELS: Record<HeaderField, string> = {
  invoice_number: "Invoice number",
  invoice_date: "Invoice date",
  supplier_name: "Supplier",
  customer_name: "Customer",
  subtotal: "Subtotal",
  tax: "Tax",
  total: "Total",
  currency: "Currency",
};

export const AMOUNT_FIELDS: ReadonlySet<string> = new Set([
  "subtotal",
  "tax",
  "total",
  "quantity",
  "unit_price",
  "amount",
  "tax_rate",
]);

const MAX_TEXT = 500;
const amount = z
  .string()
  .trim()
  .refine((v) => v === "" || /^-?\d+(\.\d{1,4})?$/.test(v), "Use a plain number, e.g. 1234.56");
const isoDate = z
  .string()
  .trim()
  .refine((v) => {
    if (v === "") {
      return true;
    }
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (!match) {
      return false;
    }
    const date = new Date(`${v}T00:00:00Z`);
    return !Number.isNaN(date.getTime()) && date.toISOString().startsWith(v);
  }, "Use YYYY-MM-DD");
const text = z.string().trim().max(MAX_TEXT, `At most ${MAX_TEXT} characters`);

export const reviewFormSchema = z.object({
  fields: z.object({
    invoice_number: text,
    invoice_date: isoDate,
    supplier_name: text,
    customer_name: text,
    subtotal: amount,
    tax: amount,
    total: amount,
    currency: z
      .string()
      .trim()
      .toUpperCase()
      .refine((v) => v === "" || /^[A-Z]{3}$/.test(v), "Use a 3-letter code, e.g. EUR"),
  }),
  lineItems: z.array(
    z.object({
      id: z.string().nullable(),
      position: z.number().nullable(),
      description: text,
      quantity: amount,
      unit_price: amount,
      amount,
      tax_rate: amount,
    }),
  ),
  reason: z.string().trim().max(2000),
});

export type ReviewFormValues = z.infer<typeof reviewFormSchema>;
export type LineItemValues = ReviewFormValues["lineItems"][number];

export function toFormValues(extraction: ExtractionOut): ReviewFormValues {
  const fields = Object.fromEntries(
    HEADER_FIELDS.map((name) => [name, extraction.fields[name]?.value ?? ""]),
  ) as ReviewFormValues["fields"];
  return {
    fields,
    lineItems: [...extraction.line_items]
      .sort((a, b) => a.position - b.position)
      .map((item) => ({
        id: item.id,
        position: item.position,
        description: item.description ?? "",
        quantity: item.quantity ?? "",
        unit_price: item.unit_price ?? "",
        amount: item.amount ?? "",
        tax_rate: item.tax_rate ?? "",
      })),
    reason: "",
  };
}

const orNull = (value: string): string | null => (value.trim() === "" ? null : value.trim());

/** Numerically equal amounts ("120" vs "120.0000") are not a change. */
function sameValue(field: string, a: string, b: string): boolean {
  if (AMOUNT_FIELDS.has(field)) {
    const x = toScaled(a);
    const y = toScaled(b);
    if (x !== null && y !== null) {
      return x === y;
    }
  }
  return a.trim() === b.trim();
}

export interface CorrectionDraft {
  request: CorrectionRequest;
  /** Form row index of each line op (-1 for deletes): maps 422 errors back to rows. */
  opRows: number[];
}

/**
 * The PATCH body: ONLY what changed (header fields, and line items as
 * add / update / delete ops). null = nothing to save.
 */
export function buildCorrection(
  initial: ReviewFormValues,
  current: ReviewFormValues,
): CorrectionDraft | null {
  const fields: Record<string, string | null> = {};
  for (const name of HEADER_FIELDS) {
    if (!sameValue(name, initial.fields[name], current.fields[name])) {
      fields[name] = orNull(current.fields[name]);
    }
  }
  const before = new Map(initial.lineItems.flatMap((row) => (row.id ? [[row.id, row] as const] : [])));
  const ops: LineItemOp[] = [];
  const opRows: number[] = [];
  current.lineItems.forEach((row, rowIndex) => {
    const original = row.id ? before.get(row.id) : undefined;
    if (!original) {
      const values = Object.fromEntries(
        LINE_FIELDS.flatMap((f) => (orNull(row[f]) === null ? [] : [[f, orNull(row[f])]])),
      );
      if (Object.keys(values).length > 0) {
        ops.push({ op: "add", ...values });
        opRows.push(rowIndex);
      }
      return;
    }
    const changed = Object.fromEntries(
      LINE_FIELDS.filter((f) => !sameValue(f, original[f], row[f])).map((f) => [f, orNull(row[f])]),
    );
    if (Object.keys(changed).length > 0) {
      ops.push({ op: "update", id: original.id, ...changed });
      opRows.push(rowIndex);
    }
  });
  const kept = new Set(current.lineItems.map((row) => row.id));
  for (const id of before.keys()) {
    if (!kept.has(id)) {
      ops.push({ op: "delete", id });
      opRows.push(-1);
    }
  }
  if (Object.keys(fields).length === 0 && ops.length === 0) {
    return null;
  }
  return {
    request: {
      ...(Object.keys(fields).length > 0 ? { fields } : {}),
      ...(ops.length > 0 ? { line_items: ops } : {}),
      ...(orNull(current.reason) ? { reason: current.reason.trim() } : {}),
    },
    opRows,
  };
}

/** UX hint: quantity x unit price vs amount (exact decimals). null when not checkable or fine. */
export function lineHint(row: LineItemValues): string | null {
  const qty = toScaled(row.quantity);
  const price = toScaled(row.unit_price);
  const total = toScaled(row.amount);
  if (qty === null || price === null || total === null) {
    return null;
  }
  const expected = multiply(qty, price);
  return absolute(expected - total) > HINT_TOLERANCE
    ? `${row.quantity} × ${row.unit_price} = ${fromScaled(expected)}, not ${row.amount}`
    : null;
}

/** UX hint: sum of line amounts vs subtotal. */
export function sumHint(values: ReviewFormValues): string | null {
  const subtotal = toScaled(values.fields.subtotal);
  const amounts = values.lineItems.map((r) => toScaled(r.amount));
  if (subtotal === null || amounts.length === 0 || amounts.some((a) => a === null)) {
    return null;
  }
  const sum = amounts.reduce<bigint>((acc, a) => acc + (a ?? 0n), 0n);
  return absolute(sum - subtotal) > HINT_TOLERANCE
    ? `Lines add up to ${fromScaled(sum)}, subtotal is ${values.fields.subtotal}`
    : null;
}

/** Issue field_path ("total", "line_items[2].amount") -> form field name. */
export function formPathFor(fieldPath: string | null, values: ReviewFormValues): string | null {
  if (!fieldPath) {
    return null;
  }
  if ((HEADER_FIELDS as readonly string[]).includes(fieldPath)) {
    return `fields.${fieldPath}`;
  }
  const match = /^line_items\[(\d+)\](?:\.(\w+))?$/.exec(fieldPath);
  if (!match) {
    return null;
  }
  const position = Number(match[1]);
  const index = values.lineItems.findIndex((row) => row.position === position);
  if (index < 0) {
    return null;
  }
  const field = match[2];
  return field && (LINE_FIELDS as readonly string[]).includes(field)
    ? `lineItems.${index}.${field}`
    : `lineItems.${index}.description`;
}

export const rejectSchema = z.object({ note: z.string().trim().min(1, "A note is required").max(2000) });
export const resolveSchema = z.object({
  note: z.string().trim().min(1, "Explain why it's resolved").max(2000),
});
