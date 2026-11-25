"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { isAppError, type AppError } from "@/lib/api/errors";
import type { UploadOutcome } from "@/lib/api/types";
import { documentKeys } from "@/features/documents/api";

import { newIdempotencyKey, uploadFiles, type UploadProgress } from "./api";

export type UploadPhase = "idle" | "uploading" | "done" | "failed";

export interface UploadState {
  phase: UploadPhase;
  progress: UploadProgress | null;
  outcomes: UploadOutcome[];
  error: AppError | null;
}

const IDLE: UploadState = { phase: "idle", progress: null, outcomes: [], error: null };

/**
 * One batch = one Idempotency-Key. The key is created when files are chosen and
 * kept across retries; choosing a new set of files starts a new batch.
 */
export function useUpload() {
  const queryClient = useQueryClient();
  const [state, setState] = useState<UploadState>(IDLE);
  const keyRef = useRef<string | null>(null);

  const start = useCallback(
    async (files: readonly File[]) => {
      keyRef.current ??= newIdempotencyKey();
      setState({ phase: "uploading", progress: { loaded: 0, total: 1 }, outcomes: [], error: null });
      try {
        const outcomes = await uploadFiles(files, keyRef.current, (progress) => {
          setState((s) => ({ ...s, progress }));
        });
        setState({ phase: "done", progress: null, outcomes, error: null });
        await queryClient.invalidateQueries({ queryKey: documentKeys.lists() });
      } catch (error) {
        setState({
          phase: "failed",
          progress: null,
          outcomes: [],
          error: isAppError(error) ? error : null,
        });
      }
    },
    [queryClient],
  );

  /** New files: a new batch with a new key. */
  const reset = useCallback(() => {
    keyRef.current = null;
    setState(IDLE);
  }, []);

  return { state, start, reset, idempotencyKey: () => keyRef.current };
}
