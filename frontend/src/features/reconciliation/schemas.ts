import { z } from "zod";

/**
 * The run report is free-form JSON in the API contract (`report: object`), so
 * it is validated here at runtime instead of being trusted by a cast.
 */
const entry = z.object({
  extraction_id: z.string(),
  document_id: z.string(),
  filename: z.string(),
  invoice_number: z.string().nullable(),
  supplier: z.string().nullable(),
  total: z.string().nullable(),
});

const difference = z.object({ field: z.string(), left: z.unknown(), right: z.unknown() });

const pair = z.object({
  left: entry,
  right: entry,
  match: z.string(),
  supplier_similarity: z.number(),
  differences: z.array(difference),
  not_compared: z.array(z.string()).default([]),
});

export const reportSchema = z.object({
  counts: z.object({
    matched: z.number(),
    mismatched: z.number(),
    only_in_left: z.number(),
    only_in_right: z.number(),
    ambiguous: z.number(),
  }),
  matched: z.array(pair),
  mismatched: z.array(pair),
  only_in_left: z.array(entry),
  only_in_right: z.array(entry),
  ambiguous: z.array(z.record(z.string(), z.array(entry))),
});

export type ReconciliationReport = z.infer<typeof reportSchema>;
export type ReportEntry = z.infer<typeof entry>;
export type ReportPair = z.infer<typeof pair>;

export const MAX_SET_SIZE = 500;
