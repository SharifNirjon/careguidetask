"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { fetchRun, reconciliationKeys, startRun } from "./api";

const RUN_POLL_MS = 2000;

export function useStartRun() {
  const router = useRouter();
  return useMutation({
    mutationFn: ({ left, right }: { left: string[]; right: string[] }) => startRun(left, right),
    onSuccess: (runId) => {
      router.push(`/reconciliation/${runId}`);
    },
  });
}

/** Polls while the run is in progress (pauses while the tab is hidden). */
export function useRun(runId: string) {
  return useQuery({
    queryKey: reconciliationKeys.run(runId),
    queryFn: () => fetchRun(runId),
    refetchInterval: (query) => (query.state.data?.status === "running" ? RUN_POLL_MS : false),
  });
}
