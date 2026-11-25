"use client";

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { documentKeys } from "@/features/documents/api";
import type { CorrectionRequest, DocumentResults, ExtractionOut, IssueOut } from "@/lib/api/types";

import {
  approveExtraction,
  correctExtraction,
  fetchCorrections,
  fetchExtraction,
  fetchUnitTable,
  fetchVersions,
  rejectExtraction,
  reopenIssue,
  resolveIssue,
  reviewKeys,
} from "./api";

/** Put a fresh extraction (from a mutation response) into the cached results. */
function storeExtraction(client: QueryClient, documentId: string, extraction: ExtractionOut): void {
  client.setQueryData<DocumentResults>(documentKeys.results(documentId), (results) =>
    results
      ? { ...results, extractions: results.extractions.map((e) => (e.id === extraction.id ? extraction : e)) }
      : results,
  );
}

/** Status and lists may change after any review action (needs_review -> completed). */
async function refreshAround(client: QueryClient, documentId: string, extractionId: string): Promise<void> {
  await Promise.all([
    client.invalidateQueries({ queryKey: documentKeys.detail(documentId) }),
    client.invalidateQueries({ queryKey: documentKeys.lists() }),
    client.invalidateQueries({ queryKey: reviewKeys.corrections(extractionId) }),
    client.invalidateQueries({ queryKey: reviewKeys.versions(extractionId) }),
  ]);
}

export function useCorrectExtraction(documentId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ extraction, body }: { extraction: ExtractionOut; body: CorrectionRequest }) =>
      correctExtraction(extraction.id, body, extraction.lock_version),
    onSuccess: async (updated) => {
      storeExtraction(client, documentId, updated);
      await refreshAround(client, documentId, updated.id);
    },
  });
}

export function useReviewDecision(documentId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      extractionId,
      decision,
      note,
    }: {
      extractionId: string;
      decision: "approve" | "reject";
      note: string | null;
    }) =>
      decision === "approve"
        ? approveExtraction(extractionId, note)
        : rejectExtraction(extractionId, note ?? ""),
    onSuccess: async (updated) => {
      storeExtraction(client, documentId, updated);
      await refreshAround(client, documentId, updated.id);
    },
  });
}

/**
 * Resolve / reopen are optimistic (the issue flips at once, and rolls back on
 * failure), then the results are refetched: the server re-validates, which can
 * change review status and other issues.
 */
export function useIssueAction(documentId: string) {
  const client = useQueryClient();
  const key = documentKeys.results(documentId);
  return useMutation({
    mutationFn: ({ issue, note }: { issue: IssueOut; note?: string }) =>
      note === undefined ? reopenIssue(issue.id) : resolveIssue(issue.id, note),
    onMutate: async ({ issue, note }) => {
      await client.cancelQueries({ queryKey: key });
      const previous = client.getQueryData<DocumentResults>(key);
      const resolved = note !== undefined;
      client.setQueryData<DocumentResults>(key, (results) =>
        results
          ? {
              ...results,
              extractions: results.extractions.map((e) => ({
                ...e,
                issues: e.issues.map((i) =>
                  i.id === issue.id ? { ...i, resolved, resolution_note: note ?? null } : i,
                ),
              })),
            }
          : results,
      );
      return { previous };
    },
    onError: (_error, _vars, context) => {
      if (context?.previous) {
        client.setQueryData(key, context.previous);
      }
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: key }),
        client.invalidateQueries({ queryKey: documentKeys.detail(documentId) }),
        client.invalidateQueries({ queryKey: documentKeys.lists() }),
      ]);
    },
  });
}

export function useVersions(extractionId: string) {
  return useQuery({
    queryKey: reviewKeys.versions(extractionId),
    queryFn: () => fetchVersions(extractionId),
  });
}

export function useCorrections(extractionId: string) {
  return useQuery({
    queryKey: reviewKeys.corrections(extractionId),
    queryFn: () => fetchCorrections(extractionId),
  });
}

export function useExtraction(extractionId: string) {
  return useQuery({
    queryKey: reviewKeys.extraction(extractionId),
    queryFn: () => fetchExtraction(extractionId),
  });
}

export function useUnitTable(documentId: string, unitIndex: number, enabled: boolean) {
  return useQuery({
    queryKey: reviewKeys.table(documentId, unitIndex),
    queryFn: () => fetchUnitTable(documentId, unitIndex),
    enabled,
    staleTime: Infinity, // parsed rows only change on reprocessing
  });
}
