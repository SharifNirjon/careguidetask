"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useRef } from "react";

import { TERMINAL_STATUSES } from "@/components/status-badges";
import { pollDelay } from "@/lib/api/etag";
import type { DocumentStatus, FromStage } from "@/lib/api/types";

import {
  documentKeys,
  fetchDocument,
  fetchDocuments,
  fetchEvents,
  fetchNextToReview,
  fetchResults,
  reprocessDocument,
} from "./api";
import { compact, parseListParams, type ListParams } from "./schemas";

const LIST_REFRESH_MS = 3000;
const ACTIVE: ReadonlySet<DocumentStatus> = new Set(["uploaded", "queued", "processing"]);

/** Filters/sort/cursor <-> URL. Changing a filter resets the cursor (page 1). */
export function useListParams(): [ListParams, (next: Partial<ListParams>) => void] {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const params = useMemo(() => parseListParams(new URLSearchParams(search.toString())), [search]);
  const update = useCallback(
    (next: Partial<ListParams>) => {
      const merged = compact({ ...params, cursor: undefined, ...next });
      const query = new URLSearchParams(Object.entries(merged));
      router.push(query.size ? `${pathname}?${query.toString()}` : pathname);
    },
    [params, pathname, router],
  );
  return [params, update];
}

export function useDocuments(params: ListParams) {
  return useQuery({
    queryKey: documentKeys.list(params),
    queryFn: () => fetchDocuments(params),
    placeholderData: keepPreviousData,
    // Keep the list live only while something on screen is still being processed.
    refetchInterval: (query) =>
      query.state.data?.items.some((d) => ACTIVE.has(d.status)) ? LIST_REFRESH_MS : false,
  });
}

/**
 * Document detail with progress. Polls while the document is processing, with
 * ETags (unchanged -> 304, no body) and backoff 1 s -> 5 s. Polling stops at a
 * terminal status, and TanStack pauses it while the tab is hidden.
 */
export function useDocument(id: string) {
  const unchanged = useRef(0);
  return useQuery({
    queryKey: documentKeys.detail(id),
    queryFn: async () => {
      const result = await fetchDocument(id);
      unchanged.current = result.changed ? 0 : unchanged.current + 1;
      return result.data;
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL_STATUSES.has(status) ? false : pollDelay(unchanged.current);
    },
    refetchIntervalInBackground: false,
  });
}

export function useResults(id: string, enabled = true) {
  return useQuery({
    queryKey: documentKeys.results(id),
    queryFn: async () => (await fetchResults(id)).data,
    enabled,
  });
}

export function useEvents(id: string) {
  return useQuery({ queryKey: documentKeys.events(id), queryFn: () => fetchEvents(id) });
}

/** "Start reviewing" / "N" shortcut: open the oldest document that needs review. */
export function useOpenNextToReview() {
  const router = useRouter();
  return useMutation({
    mutationFn: (excludeId?: string) => fetchNextToReview(excludeId),
    onSuccess: (id) => {
      if (id) {
        router.push(`/documents/${id}`);
      }
    },
  });
}

export function useReprocess(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ fromStage, force }: { fromStage: FromStage; force: boolean }) =>
      reprocessDocument(id, fromStage, force),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: documentKeys.detail(id) }),
        queryClient.invalidateQueries({ queryKey: documentKeys.lists() }),
      ]);
    },
  });
}
