import { newRequestId, proxyUrl } from "@/lib/api/client";
import { networkError, toAppError } from "@/lib/api/errors";
import type { UploadOutcome } from "@/lib/api/types";

export interface UploadProgress {
  loaded: number;
  total: number;
}

/**
 * Multipart upload through the proxy. XHR rather than fetch because only XHR
 * reports upload progress. The Idempotency-Key is chosen once per batch by the
 * caller and reused on retry, so a retry after a dropped connection returns the
 * original outcomes instead of ingesting the files twice.
 */
export function uploadFiles(
  files: readonly File[],
  idempotencyKey: string,
  onProgress: (progress: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<UploadOutcome[]> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const file of files) {
      form.append("files", file, file.name);
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", proxyUrl("/api/v1/documents"));
    xhr.setRequestHeader("Idempotency-Key", idempotencyKey);
    xhr.setRequestHeader("X-Request-ID", newRequestId());
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress({ loaded: event.loaded, total: event.total });
      }
    };
    xhr.onload = () => {
      const body: unknown = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300 && Array.isArray(body)) {
        resolve(body as UploadOutcome[]); // shape guaranteed by the generated contract
      } else {
        reject(toAppError(xhr.status, body, xhr.getResponseHeader("x-request-id")));
      }
    };
    xhr.onerror = () => {
      reject(networkError(new Error("upload failed")));
    };
    xhr.onabort = () => {
      reject(networkError(new Error("upload aborted")));
    };
    signal?.addEventListener(
      "abort",
      () => {
        xhr.abort();
      },
      { once: true },
    );
    xhr.send(form);
  });
}

export function newIdempotencyKey(): string {
  return `upload-${crypto.randomUUID()}`;
}
