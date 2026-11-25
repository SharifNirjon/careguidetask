import { z } from "zod";

import type { SearchMode } from "@/lib/api/types";

export const SEARCH_MODES = ["keyword", "semantic", "hybrid"] as const;
export const DEFAULT_MODE: SearchMode = "hybrid";
export const MAX_QUERY_CHARS = 500;
export const SEARCH_PAGE_SIZE = 20;

/** Typing pauses before a search runs. Semantic/hybrid spend LLM quota, so they wait longer. */
export const KEYWORD_DEBOUNCE_MS = 300;
export const SEMANTIC_IDLE_MS = 600;
export const SEMANTIC_MIN_CHARS = 3;

/**
 * When may this query run automatically? null = never (only explicit submit
 * may run it, and even that is refused for semantic/hybrid under 3 chars).
 */
export function autoSearchDelay(mode: SearchMode, query: string): number | null {
  const q = query.trim();
  if (q.length === 0) {
    return null;
  }
  if (mode === "keyword") {
    return KEYWORD_DEBOUNCE_MS;
  }
  return q.length >= SEMANTIC_MIN_CHARS ? SEMANTIC_IDLE_MS : null;
}

/** May an explicit submit (Enter) run this query? */
export function canSubmit(mode: SearchMode, query: string): boolean {
  const q = query.trim();
  return q.length > 0 && (mode === "keyword" || q.length >= SEMANTIC_MIN_CHARS);
}

export const searchParamsSchema = z.object({
  q: z.string().max(MAX_QUERY_CHARS).optional().catch(undefined),
  mode: z.enum(SEARCH_MODES).default(DEFAULT_MODE).catch(DEFAULT_MODE),
  offset: z.coerce.number().int().min(0).max(200).default(0).catch(0),
});
