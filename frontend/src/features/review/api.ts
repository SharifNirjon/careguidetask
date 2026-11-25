import { api } from "@/lib/api/client";
import type { CorrectionRequest, ExtractionOut, IssueOut } from "@/lib/api/types";

export const reviewKeys = {
  all: ["review"] as const,
  versions: (extractionId: string) => [...reviewKeys.all, "versions", extractionId] as const,
  corrections: (extractionId: string) => [...reviewKeys.all, "corrections", extractionId] as const,
  extraction: (extractionId: string) => [...reviewKeys.all, "extraction", extractionId] as const,
  table: (documentId: string, unit: number) => [...reviewKeys.all, "table", documentId, unit] as const,
};

function required<T>(data: T | undefined, what: string): T {
  if (data === undefined) {
    throw new Error(`Empty ${what} response`);
  }
  return data;
}

/** PATCH with If-Match = lock_version (412 if someone else saved first). */
export async function correctExtraction(
  id: string,
  body: CorrectionRequest,
  lockVersion: number,
): Promise<ExtractionOut> {
  const { data } = await api().PATCH("/api/v1/extractions/{extraction_id}", {
    params: { path: { extraction_id: id }, header: { "If-Match": String(lockVersion) } },
    body,
  });
  return required(data, "correction");
}

export async function approveExtraction(id: string, note: string | null): Promise<ExtractionOut> {
  const { data } = await api().POST("/api/v1/extractions/{extraction_id}/approve", {
    params: { path: { extraction_id: id } },
    body: { note },
  });
  return required(data, "approve");
}

export async function rejectExtraction(id: string, note: string): Promise<ExtractionOut> {
  const { data } = await api().POST("/api/v1/extractions/{extraction_id}/reject", {
    params: { path: { extraction_id: id } },
    body: { note },
  });
  return required(data, "reject");
}

export async function resolveIssue(id: string, note: string): Promise<IssueOut> {
  const { data } = await api().POST("/api/v1/issues/{issue_id}/resolve", {
    params: { path: { issue_id: id } },
    body: { note },
  });
  return required(data, "resolve");
}

export async function reopenIssue(id: string): Promise<IssueOut> {
  const { data } = await api().POST("/api/v1/issues/{issue_id}/reopen", {
    params: { path: { issue_id: id } },
  });
  return required(data, "reopen");
}

export async function fetchVersions(extractionId: string) {
  const { data } = await api().GET("/api/v1/extractions/{extraction_id}/versions", {
    params: { path: { extraction_id: extractionId } },
  });
  return required(data, "versions");
}

export async function fetchCorrections(extractionId: string) {
  const { data } = await api().GET("/api/v1/extractions/{extraction_id}/corrections", {
    params: { path: { extraction_id: extractionId } },
  });
  return required(data, "corrections");
}

export async function fetchExtraction(extractionId: string): Promise<ExtractionOut> {
  const { data } = await api().GET("/api/v1/extractions/{extraction_id}", {
    params: { path: { extraction_id: extractionId } },
  });
  return required(data, "extraction");
}

export async function fetchUnitTable(documentId: string, unitIndex: number) {
  const { data } = await api().GET("/api/v1/documents/{document_id}/units/{unit_index}/table", {
    params: { path: { document_id: documentId, unit_index: unitIndex } },
  });
  return required(data, "table");
}
