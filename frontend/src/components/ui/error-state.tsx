"use client";

import { AlertTriangleIcon, RotateCwIcon } from "lucide-react";

import { isAppError } from "@/lib/api/errors";

import { Button } from "./button";

/** Error with a retry action and the request id (support can grep the logs for it). */
export function ErrorState({
  error,
  onRetry,
  title = "Couldn't load this",
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  const appError = isAppError(error) ? error : null;
  return (
    <div
      role="alert"
      className="border-destructive/40 bg-destructive/5 flex flex-col items-start gap-3 rounded-lg border p-4"
    >
      <div className="flex items-center gap-2 font-medium">
        <AlertTriangleIcon className="text-destructive size-4" aria-hidden="true" />
        {title}
      </div>
      <p className="text-sm">{appError?.userMessage ?? "Something went wrong. Please try again."}</p>
      {appError?.requestId ? (
        <details className="text-muted-foreground text-xs">
          <summary className="cursor-pointer">Details</summary>
          <p>
            Error code <code>{appError.code}</code> · request id <code>{appError.requestId}</code>
          </p>
        </details>
      ) : null}
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCwIcon aria-hidden="true" /> Try again
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-lg border border-dashed p-10 text-center">
      <p className="font-medium">{title}</p>
      {children}
    </div>
  );
}
