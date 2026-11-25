import { api } from "@/lib/api/client";
import type { SearchMode, SearchResponse } from "@/lib/api/types";
import { compact, type DocumentFilters } from "@/features/documents/schemas";

import { SEARCH_PAGE_SIZE } from "./schemas";

export interface SearchArgs {
  q: string;
  mode: SearchMode;
  offset: number;
  filters: DocumentFilters;
}

export const searchKeys = {
  all: ["search"] as const,
  query: (args: SearchArgs) => [...searchKeys.all, { ...args, filters: compact(args.filters) }] as const,
};

export async function search({ q, mode, offset, filters }: SearchArgs): Promise<SearchResponse> {
  const { has_open_issues, ...rest } = compact(filters);
  const { data } = await api().GET("/api/v1/search", {
    params: {
      query: {
        ...rest,
        ...(has_open_issues === undefined ? {} : { has_open_issues: has_open_issues === "true" }),
        q,
        mode,
        offset,
        limit: SEARCH_PAGE_SIZE,
      },
    },
  });
  if (!data) {
    throw new Error("Empty search response");
  }
  return data;
}
