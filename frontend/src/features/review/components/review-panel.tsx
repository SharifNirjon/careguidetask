"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { SaveIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FormProvider, useForm, type FieldPath } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/form-controls";
import { Card } from "@/components/ui/misc";
import { useOpenNextToReview } from "@/features/documents/hooks";
import { forgetEtag } from "@/lib/api/etag";
import { isAppError, type AppError } from "@/lib/api/errors";
import type { DocumentDetail, ExtractionOut, IssueOut } from "@/lib/api/types";

import { useCorrectExtraction, useReviewDecision } from "../hooks";
import {
  buildCorrection,
  formPathFor,
  HEADER_FIELDS,
  LINE_FIELDS,
  reviewFormSchema,
  toFormValues,
  type ReviewFormValues,
} from "../schemas";
import { useShortcuts } from "../use-shortcuts";
import { useUnsavedGuard } from "../use-unsaved-guard";
import { ConflictDialog } from "./conflict-dialog";
import { FieldRow } from "./field-row";
import { IssuesPanel } from "./issues-panel";
import { LineItemsTable } from "./line-items-table";
import { approveBlocker, ReviewHeader } from "./review-header";
import { ReviewTabs } from "./review-tabs";
import { ShortcutsHelp } from "./shortcuts-help";

const SEVERITY_RANK = { error: 0, warning: 1, info: 2 } as const;

export interface ReviewPanelProps {
  document: DocumentDetail;
  extraction: ExtractionOut;
  onShowOnPage: (unitIndex: number, evidence: string | null) => void;
  onRefetchResults: () => Promise<unknown>;
}

/** Field names the server's 422 errors point at -> form paths. */
function applyServerErrors(
  error: AppError,
  opRows: number[],
  setError: (path: FieldPath<ReviewFormValues>, msg: string) => void,
): number {
  let count = 0;
  for (const [field, msg] of Object.entries(error.fieldMessages(["body", "fields"]))) {
    if ((HEADER_FIELDS as readonly string[]).includes(field)) {
      setError(`fields.${field}` as FieldPath<ReviewFormValues>, msg);
      count += 1;
    }
  }
  for (const [path, msg] of Object.entries(error.fieldMessages(["body", "line_items"]))) {
    const [op, field] = path.split(".");
    const row = opRows[Number(op)];
    if (row !== undefined && row >= 0 && field && (LINE_FIELDS as readonly string[]).includes(field)) {
      setError(`lineItems.${row}.${field}` as FieldPath<ReviewFormValues>, msg);
      count += 1;
    }
  }
  return count;
}

export function ReviewPanel({ document, extraction, onShowOnPage, onRefetchResults }: ReviewPanelProps) {
  const form = useForm<ReviewFormValues>({
    resolver: zodResolver(reviewFormSchema),
    defaultValues: toFormValues(extraction),
  });
  const { reset, setValue, setError, setFocus, getValues, formState } = form;
  const correct = useCorrectExtraction(document.id);
  const decision = useReviewDecision(document.id);
  const openNext = useOpenNextToReview();
  const confirmLeave = useUnsavedGuard(formState.isDirty);
  const [announcement, setAnnouncement] = useState("");
  const [conflict, setConflict] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [activeIssue, setActiveIssue] = useState<string | null>(null);

  const base = useRef(toFormValues(extraction)); // what the PATCH diff is computed against
  const justSaved = useRef(false);
  const reapply = useRef<{ mine: ReviewFormValues; base: ReviewFormValues } | null>(null);

  // A new server version arrived (our save, someone else's, or re-validation).
  useEffect(() => {
    const fresh = toFormValues(extraction);
    const pending = reapply.current;
    if (pending) {
      reset(fresh);
      for (const name of HEADER_FIELDS) {
        if (pending.mine.fields[name] !== pending.base.fields[name]) {
          setValue(`fields.${name}`, pending.mine.fields[name], { shouldDirty: true });
        }
      }
      if (JSON.stringify(pending.mine.lineItems) !== JSON.stringify(pending.base.lineItems)) {
        setValue("lineItems", pending.mine.lineItems, { shouldDirty: true });
      }
      reapply.current = null;
      toast.info("Your changes were re-applied on the latest version. Review them and save.");
    } else if (justSaved.current) {
      reset(fresh); // clean form showing exactly what the server stored
      justSaved.current = false;
    } else {
      reset(fresh, { keepDirtyValues: true }); // never throw away edits in progress
    }
    base.current = fresh;
  }, [extraction, reset, setValue]);

  const issues = useMemo(
    () => [...extraction.issues].sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]),
    [extraction.issues],
  );

  const onValid = (values: ReviewFormValues) => {
    const draft = buildCorrection(base.current, values);
    if (!draft) {
      toast.info("No changes to save.");
      return;
    }
    correct.mutate(
      { extraction, body: draft.request },
      {
        onSuccess: () => {
          justSaved.current = true;
          setAnnouncement("Saved. Issues and status were re-checked.");
          toast.success("Saved");
        },
        onError: (error) => {
          if (!isAppError(error)) {
            toast.error("Couldn't save.");
          } else if (error.status === 412 || error.status === 428) {
            setConflict(true);
          } else if (
            error.status === 422 &&
            applyServerErrors(error, draft.opRows, (p, m) => {
              setError(p, { message: m });
            }) > 0
          ) {
            setAnnouncement("Some values were rejected. Check the highlighted fields.");
            toast.error(error.userMessage);
          } else {
            toast.error(error.userMessage, {
              description: error.requestId ? `Request id ${error.requestId}` : undefined,
            });
          }
        },
      },
    );
  };

  const onInvalid = () => {
    setAnnouncement("Fix the highlighted fields before saving.");
    toast.error("Fix the highlighted fields before saving.");
  };

  // handleSubmit is called inside the event handler, so the refs it reads are
  // read at event time, never during render.
  const save = (event?: React.BaseSyntheticEvent) => form.handleSubmit(onValid, onInvalid)(event);

  const focusIssueField = useCallback(
    (issue: IssueOut, focus: boolean) => {
      setActiveIssue(issue.id);
      const path = formPathFor(issue.field_path, getValues());
      const headerField = issue.field_path && extraction.fields[issue.field_path];
      if (headerField && headerField.unit_index !== null && headerField.unit_index !== undefined) {
        onShowOnPage(headerField.unit_index, headerField.evidence ?? null);
      }
      if (path) {
        const element = globalThis.document.getElementsByName(path)[0];
        element?.scrollIntoView({ block: "center" });
        if (focus) {
          setFocus(path as FieldPath<ReviewFormValues>);
        }
      }
    },
    [extraction.fields, getValues, onShowOnPage, setFocus],
  );

  const stepIssue = (step: 1 | -1) => {
    if (issues.length === 0) {
      return;
    }
    const current = issues.findIndex((i) => i.id === activeIssue);
    const next = issues[(current + step + issues.length) % issues.length];
    if (next) {
      focusIssueField(next, false); // J/K keep focus on the page so J/K keep working
    }
  };

  const approve = () => {
    const blocker = approveBlocker(extraction, formState.isDirty);
    if (blocker) {
      toast.error(blocker);
      return;
    }
    decision.mutate(
      { extractionId: extraction.id, decision: "approve", note: null },
      {
        onSuccess: () => {
          setAnnouncement("Invoice approved.");
          toast.success("Approved");
        },
        onError: (e) => toast.error(isAppError(e) ? e.userMessage : "Couldn't approve"),
      },
    );
  };

  useShortcuts({
    save: () => void save(),
    nextIssue: () => {
      stepIssue(1);
    },
    previousIssue: () => {
      stepIssue(-1);
    },
    approve,
    nextDocument: () => {
      if (confirmLeave()) {
        openNext.mutate(document.id, {
          onSuccess: (id) => (id ? undefined : toast.success("Nothing else needs review.")),
        });
      }
    },
    help: () => {
      setHelpOpen(true);
    },
  });

  return (
    <FormProvider {...form}>
      <div className="space-y-4">
        <ReviewHeader
          document={document}
          extraction={extraction}
          dirty={formState.isDirty}
          deciding={decision.isPending}
          onApprove={approve}
          onReject={(note) => {
            decision.mutate(
              { extractionId: extraction.id, decision: "reject", note },
              {
                onSuccess: () => toast.success("Rejected"),
                onError: (e) => toast.error(isAppError(e) ? e.userMessage : "Couldn't reject"),
              },
            );
          }}
        />
        <p className="sr-only" aria-live="polite">
          {announcement}
        </p>

        <Card aria-labelledby="issues-heading">
          <h2 id="issues-heading" className="mb-3 font-semibold">
            Issues
          </h2>
          <IssuesPanel
            issues={issues}
            activeId={activeIssue}
            documentId={document.id}
            onActivate={(issue) => {
              focusIssueField(issue, true);
            }}
            canApply={(issue) =>
              issue.expected_value !== null && formPathFor(issue.field_path, getValues()) !== null
            }
            onApply={(issue) => {
              const path = formPathFor(issue.field_path, getValues());
              if (path && issue.expected_value !== null) {
                setValue(path as FieldPath<ReviewFormValues>, issue.expected_value, {
                  shouldDirty: true,
                  shouldValidate: true,
                });
                setFocus(path as FieldPath<ReviewFormValues>);
                toast.info("Suggestion applied. Save to keep it.");
              }
            }}
          />
        </Card>

        <form onSubmit={(e) => void save(e)} noValidate aria-label="Invoice fields">
          <Card className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold">Fields</h2>
              <Button type="submit" size="sm" disabled={!formState.isDirty || correct.isPending}>
                <SaveIcon aria-hidden="true" /> {correct.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
            <div className="divide-border divide-y">
              {HEADER_FIELDS.map((name) => (
                <FieldRow
                  key={name}
                  name={name}
                  field={extraction.fields[name]}
                  onShowOnPage={(unit) => {
                    onShowOnPage(unit, extraction.fields[name]?.evidence ?? null);
                  }}
                />
              ))}
            </div>
            <LineItemsTable />
            <div className="space-y-1">
              <Label htmlFor="reason">Reason for the change (optional, kept in the audit trail)</Label>
              <Input id="reason" {...form.register("reason")} />
            </div>
          </Card>
        </form>

        <Card>
          <ReviewTabs extraction={extraction} documentId={document.id} />
        </Card>
      </div>
      <ConflictDialog
        open={conflict}
        onCancel={() => {
          setConflict(false);
        }}
        onReload={() => {
          setConflict(false);
          reset(base.current); // drop my edits; the refetch brings theirs
          forgetEtag(`results:${document.id}`);
          void onRefetchResults();
        }}
        onKeepMine={() => {
          setConflict(false);
          reapply.current = { mine: getValues(), base: base.current };
          forgetEtag(`results:${document.id}`);
          void onRefetchResults();
        }}
      />
      <ShortcutsHelp open={helpOpen} onOpenChange={setHelpOpen} />
    </FormProvider>
  );
}
