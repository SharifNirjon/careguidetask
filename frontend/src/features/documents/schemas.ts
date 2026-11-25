import { z } from "zod";

/**
 * List filters live in the URL (shareable, back-button safe). This schema is the
 * single translation between URLSearchParams and the API's query parameters;
 * anything malformed in a hand-edited URL is dropped rather than sent.
 */
const optionalText = z.string().trim().min(1).max(200).optional().catch(undefined);
const isoDate = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/)
  .optional()
  .catch(undefined);
const decimal = z
  .string()
  .regex(/^-?\d+(\.\d+)?$/)
  .optional()
  .catch(undefined);

export const SORT_FIELDS = ["created_at", "invoice_date", "total"] as const;
export const DOCUMENT_STATUSES = [
  "uploaded",
  "queued",
  "processing",
  "completed",
  "needs_review",
  "failed",
] as const;
export const REVIEW_STATUSES = ["pending", "auto_approved", "needs_review", "approved", "rejected"] as const;
export const FILE_TYPES = ["pdf", "jpeg", "png", "tiff", "xlsx", "xls", "csv"] as const;

export const documentFiltersSchema = z.object({
  status: z.enum(DOCUMENT_STATUSES).optional().catch(undefined),
  review_status: z.enum(REVIEW_STATUSES).optional().catch(undefined),
  file_type: z.enum(FILE_TYPES).optional().catch(undefined),
  supplier: optionalText,
  customer: optionalText,
  invoice_number: optionalText,
  invoice_date_from: isoDate,
  invoice_date_to: isoDate,
  total_min: decimal,
  total_max: decimal,
  currency: z
    .string()
    .regex(/^[A-Za-z]{3}$/)
    .optional()
    .catch(undefined),
  has_open_issues: z.enum(["true", "false"]).optional().catch(undefined),
});

export type DocumentFilters = z.infer<typeof documentFiltersSchema>;

/** Every filter unset (sort and order are kept). */
export const NO_FILTERS: DocumentFilters = Object.fromEntries(
  Object.keys(documentFiltersSchema.shape).map((key) => [key, undefined]),
);

export const listParamsSchema = documentFiltersSchema.extend({
  sort: z.enum(SORT_FIELDS).default("created_at").catch("created_at"),
  order: z.enum(["asc", "desc"]).default("desc").catch("desc"),
  cursor: z.string().max(512).optional().catch(undefined),
});

export type ListParams = z.infer<typeof listParamsSchema>;

export function parseListParams(search: URLSearchParams): ListParams {
  return listParamsSchema.parse(Object.fromEntries(search.entries()));
}

/** Filter-only view (search reuses the same filters without sort/cursor). */
export function parseFilters(search: URLSearchParams): DocumentFilters {
  return documentFiltersSchema.parse(Object.fromEntries(search.entries()));
}

/** Drop empty values so URLs and query keys stay canonical. */
export function compact<T extends Record<string, unknown>>(values: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(values).filter(([, v]) => v !== undefined && v !== null && v !== ""),
  ) as Partial<T>;
}

export function hasActiveFilters(filters: DocumentFilters): boolean {
  return Object.keys(compact(filters)).length > 0;
}
