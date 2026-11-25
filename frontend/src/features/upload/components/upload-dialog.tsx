"use client";

import { FileIcon, UploadIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { useDropzone } from "react-dropzone";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogTrigger } from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { formatBytes } from "@/lib/format/dates";
import { cn } from "@/lib/utils";

import { batchHint, fileHint } from "../hints";
import { useUpload } from "../hooks";
import { UploadOutcomes } from "./upload-outcomes";

export function UploadDialog() {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const { state, start, reset } = useUpload();

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    multiple: true,
    onDrop: (accepted) => {
      reset(); // a new selection is a new batch (new Idempotency-Key)
      setFiles((current) => [...current, ...accepted]);
    },
  });

  const close = (next: boolean) => {
    setOpen(next);
    if (!next && state.phase !== "uploading") {
      setFiles([]);
      reset();
    }
  };

  const percent = state.progress
    ? Math.round((100 * state.progress.loaded) / Math.max(state.progress.total, 1))
    : 0;
  const batchWarning = batchHint(files.length);

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogTrigger asChild>
        <Button>
          <UploadIcon aria-hidden="true" /> Upload
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Upload documents"
        description="PDF, PNG, JPEG, TIFF, XLSX, XLS or CSV."
        className="max-w-2xl"
      >
        {state.phase === "done" ? (
          <UploadOutcomes outcomes={state.outcomes} />
        ) : (
          <>
            <div
              {...getRootProps({
                className: cn(
                  "flex cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed border-border p-8 text-center focus-visible:outline-2 focus-visible:outline-ring",
                  isDragActive && "border-primary bg-primary/5",
                ),
              })}
            >
              <input {...getInputProps({ "aria-label": "Choose files to upload" })} />
              <UploadIcon className="text-muted-foreground size-6" aria-hidden="true" />
              <p className="text-sm">
                {isDragActive ? "Drop the files here" : "Drag files here, or press Enter to choose"}
              </p>
            </div>

            {files.length > 0 ? (
              <ul className="max-h-64 space-y-1 overflow-y-auto" aria-label="Selected files">
                {files.map((file, index) => {
                  const hint = fileHint(file);
                  return (
                    <li
                      key={`${file.name}-${index}`}
                      className="border-border flex items-start gap-2 rounded border px-3 py-2 text-sm"
                    >
                      <FileIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{file.name}</p>
                        <p className="text-muted-foreground text-xs">{formatBytes(file.size)}</p>
                        {hint ? <p className="text-warning text-xs">{hint}</p> : null}
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove ${file.name}`}
                        disabled={state.phase === "uploading"}
                        onClick={() => {
                          reset();
                          setFiles((current) => current.filter((_, i) => i !== index));
                        }}
                      >
                        <XIcon />
                      </Button>
                    </li>
                  );
                })}
              </ul>
            ) : null}
            {batchWarning ? <p className="text-warning text-sm">{batchWarning}</p> : null}

            {state.phase === "uploading" ? (
              <div className="space-y-1">
                <progress className="w-full" max={100} value={percent} aria-label="Upload progress" />
                <p className="text-sm" aria-live="polite">
                  Uploading… {percent}%
                </p>
              </div>
            ) : null}
            {state.phase === "failed" ? (
              <ErrorState
                error={state.error}
                title="Upload failed"
                // Same files, same Idempotency-Key: a retry can't create duplicates.
                onRetry={() => void start(files)}
              />
            ) : null}
          </>
        )}
        <DialogFooter>
          {state.phase === "done" ? (
            <Button
              variant="outline"
              onClick={() => {
                setFiles([]);
                reset();
              }}
            >
              Upload more
            </Button>
          ) : (
            <Button
              disabled={files.length === 0 || state.phase === "uploading"}
              onClick={() => void start(files)}
            >
              Upload {files.length > 0 ? `${files.length} file${files.length === 1 ? "" : "s"}` : ""}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
