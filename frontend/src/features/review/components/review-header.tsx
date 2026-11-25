"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { RefreshCwIcon, ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import type { z } from "zod";

import { DocumentStatusBadge, ReviewStatusBadge } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogTrigger } from "@/components/ui/dialog";
import { FieldMessage, Label, NativeSelect, Textarea } from "@/components/ui/form-controls";
import { useReprocess } from "@/features/documents/hooks";
import { isAppError } from "@/lib/api/errors";
import type { DocumentDetail, ExtractionOut, FromStage } from "@/lib/api/types";
import { formatDuration } from "@/lib/format/dates";

import { rejectSchema } from "../schemas";

const STAGES: ReadonlyArray<{ value: FromStage; label: string; hint: string }> = [
  {
    value: "validate",
    label: "Validation only",
    hint: "Re-run the checks on the current values (no quota).",
  },
  { value: "extract", label: "Extraction", hint: "Ask the model again (spends LLM quota)." },
  { value: "parse", label: "Everything", hint: "Parse the file again, then extract and validate." },
];

function ReprocessDialog({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false);
  const [stage, setStage] = useState<FromStage>("validate");
  const reprocess = useReprocess(documentId);
  const hint = STAGES.find((s) => s.value === stage)?.hint;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <RefreshCwIcon aria-hidden="true" /> Reprocess
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Reprocess document"
        description="Unsaved edits on this screen are not part of reprocessing."
      >
        <div className="space-y-1">
          <Label htmlFor="reprocess-stage">Start from</Label>
          <NativeSelect
            id="reprocess-stage"
            value={stage}
            aria-describedby="reprocess-hint"
            onChange={(e) => {
              setStage(STAGES.find((s) => s.value === e.target.value)?.value ?? "validate");
            }}
          >
            {STAGES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </NativeSelect>
          <p id="reprocess-hint" className="text-muted-foreground text-xs">
            {hint}
          </p>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              setOpen(false);
            }}
          >
            Cancel
          </Button>
          <Button
            disabled={reprocess.isPending}
            onClick={() => {
              reprocess.mutate(
                { fromStage: stage, force: false },
                {
                  onSuccess: () => {
                    toast.success("Reprocessing started");
                    setOpen(false);
                  },
                  onError: (e) => toast.error(isAppError(e) ? e.userMessage : "Couldn't start reprocessing"),
                },
              );
            }}
          >
            Reprocess
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RejectDialog({ onReject, pending }: { onReject: (note: string) => void; pending: boolean }) {
  const [open, setOpen] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<z.infer<typeof rejectSchema>>({ resolver: zodResolver(rejectSchema) });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        reset();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <ThumbsDownIcon aria-hidden="true" /> Reject
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Reject invoice"
        description="Rejected invoices are excluded from approval; the note is kept in the audit trail."
      >
        <form
          className="space-y-3"
          onSubmit={handleSubmit(({ note }) => {
            onReject(note);
            setOpen(false);
          })}
        >
          <div className="space-y-1">
            <Label htmlFor="reject-note">Reason</Label>
            <Textarea
              id="reject-note"
              autoFocus
              aria-invalid={errors.note ? true : undefined}
              aria-describedby={errors.note ? "reject-note-error" : undefined}
              {...register("note")}
            />
            {errors.note ? <FieldMessage id="reject-note-error">{errors.note.message}</FieldMessage> : null}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" variant="destructive" disabled={pending}>
              Reject
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Why Approve is disabled, or null when it isn't. */
export function approveBlocker(extraction: ExtractionOut, dirty: boolean): string | null {
  const openErrors = extraction.issues.filter((i) => i.severity === "error" && !i.resolved).length;
  if (openErrors > 0) {
    return `${openErrors} unresolved error${openErrors === 1 ? "" : "s"}: fix the values or resolve the issues first.`;
  }
  if (dirty) {
    return "Save your changes before approving.";
  }
  if (extraction.review_status === "approved") {
    return "Already approved.";
  }
  return null;
}

export function ReviewHeader({
  document,
  extraction,
  dirty,
  onApprove,
  onReject,
  deciding,
}: {
  document: DocumentDetail;
  extraction: ExtractionOut;
  dirty: boolean;
  onApprove: () => void;
  onReject: (note: string) => void;
  deciding: boolean;
}) {
  const blocker = approveBlocker(extraction, dirty);
  return (
    <div className="border-border space-y-3 border-b pb-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-xl font-semibold" title={document.filename}>
            {document.filename}
          </h1>
          <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm">
            <div className="flex gap-1">
              <dt className="sr-only">Document status</dt>
              <dd>
                <DocumentStatusBadge status={document.status} />
              </dd>
            </div>
            <div className="flex gap-1">
              <dt className="sr-only">Review status</dt>
              <dd>
                <ReviewStatusBadge status={extraction.review_status} />
              </dd>
            </div>
            <div className="flex gap-1">
              <dt className="text-muted-foreground">Confidence</dt>
              <dd className="tabular-nums">
                {extraction.overall_confidence === null
                  ? "—"
                  : `${Math.round(extraction.overall_confidence * 100)}%`}
              </dd>
            </div>
            <div className="flex gap-1">
              <dt className="text-muted-foreground">Processing</dt>
              <dd className="tabular-nums">{formatDuration(document.progress.elapsed_ms)}</dd>
            </div>
          </dl>
        </div>
        <div className="flex flex-wrap gap-2">
          <ReprocessDialog documentId={document.id} />
          <RejectDialog onReject={onReject} pending={deciding} />
          <Button
            size="sm"
            onClick={onApprove}
            disabled={blocker !== null || deciding}
            aria-describedby={blocker ? "approve-blocker" : undefined}
          >
            <ThumbsUpIcon aria-hidden="true" /> Approve
          </Button>
        </div>
      </div>
      {blocker ? (
        <p id="approve-blocker" className="text-muted-foreground text-sm">
          Approve is unavailable: {blocker}
        </p>
      ) : null}
    </div>
  );
}
