import { api } from "@/lib/api/client";
import { conditionalGet, type ConditionalResult } from "@/lib/api/etag";
import type { paths } from "@/lib/api/schema";
import type { DocumentDetail, DocumentPage, DocumentResults, FromStage, JobOut } from "@/lib/api/types";

import { compact, type ListParams } from "./schemas";

export const PAGE_SIZE = 25;

/** Query-key factory: every key for this feature comes from here (targeted invalidation). */
export const documentKeys = {
  all: ["documents"] as const,
  lists: () => [...documentKeys.all, "list"] as const,
  list: (params: ListParams) => [...documentKeys.lists(), compact(params)] as const,
  detail: (id: string) => [...documentKeys.all, "detail", id] as const,
  results: (id: string) => [...documentKeys.all, "results", id] as const,
  events: (id: string) => [...documentKeys.all, "events", id] as const,
  nextToReview: () => [...documentKeys.all, "next-to-review"] as const,
};

type ListQuery = NonNullable<paths["/api/v1/documents"]["get"]["parameters"]["query"]>;

/** URL params (strings) -> the API's typed query. */
export function toListQuery(params: ListParams): ListQuery {
  const { has_open_issues, ...rest } = compact(params);
  return {
    ...rest,
    ...(has_open_issues === undefined ? {} : { has_open_issues: has_open_issues === "true" }),
    limit: PAGE_SIZE,
  };
}

export async function fetchDocuments(params: ListParams): Promise<DocumentPage> {
  const { data } = await api().GET("/api/v1/documents", { params: { query: toListQuery(params) } });
  if (!data) {
    throw new Error("Empty document list response");
  }
  return data;
}

export function fetchDocument(id: string): Promise<ConditionalResult<DocumentDetail>> {
  return conditionalGet(`detail:${id}`, (headers) =>
    api().GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id } }, headers }),
  );
}

export function fetchResults(id: string): Promise<ConditionalResult<DocumentResults>> {
  return conditionalGet(`results:${id}`, (headers) =>
    api().GET("/api/v1/documents/{document_id}/results", { params: { path: { document_id: id } }, headers }),
  );
}

export async function fetchEvents(id: string) {
  const { data } = await api().GET("/api/v1/documents/{document_id}/events", {
    params: { path: { document_id: id }, query: { limit: 100 } },
  });
  return data?.items ?? [];
}

/** The oldest document that still needs a human ("Start reviewing"). */
export async function fetchNextToReview(excludeId?: string): Promise<string | null> {
  const { data } = await api().GET("/api/v1/documents", {
    params: { query: { status: "needs_review", sort: "created_at", order: "asc", limit: 2 } },
  });
  return data?.items.find((d) => d.id !== excludeId)?.id ?? null;
}

export async function reprocessDocument(id: string, fromStage: FromStage, force: boolean): Promise<JobOut> {
  const { data } = await api().POST("/api/v1/documents/{document_id}/reprocess", {
    params: { path: { document_id: id } },
    body: { from_stage: fromStage, force },
  });
  if (!data) {
    throw new Error("Empty reprocess response");
  }
  return data;
}
