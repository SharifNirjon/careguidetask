import { api } from "@/lib/api/client";
import type { RunOut } from "@/lib/api/types";

export const reconciliationKeys = {
  all: ["reconciliation"] as const,
  run: (runId: string) => [...reconciliationKeys.all, "run", runId] as const,
};

export async function startRun(left: string[], right: string[]): Promise<string> {
  const { data } = await api().POST("/api/v1/reconciliations", {
    body: { left_document_ids: left, right_document_ids: right },
  });
  if (!data) {
    throw new Error("Empty reconciliation response");
  }
  return data.run_id;
}

export async function fetchRun(runId: string): Promise<RunOut> {
  const { data } = await api().GET("/api/v1/reconciliations/{run_id}", {
    params: { path: { run_id: runId } },
  });
  if (!data) {
    throw new Error("Empty run response");
  }
  return data;
}
