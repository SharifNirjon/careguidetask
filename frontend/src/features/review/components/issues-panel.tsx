"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { CheckIcon, RotateCcwIcon, WandIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import type { z } from "zod";

import { SeverityBadge, severityLabel } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { FieldMessage, Label, Textarea } from "@/components/ui/form-controls";
import { isAppError } from "@/lib/api/errors";
import type { IssueOut, Severity } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { useIssueAction } from "../hooks";
import { resolveSchema } from "../schemas";

const ORDER: readonly Severity[] = ["error", "warning", "info"];

function ResolveDialog({
  issue,
  onClose,
  documentId,
}: {
  issue: IssueOut;
  onClose: () => void;
  documentId: string;
}) {
  const action = useIssueAction(documentId);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<z.infer<typeof resolveSchema>>({ resolver: zodResolver(resolveSchema) });
  const submit = handleSubmit(({ note }) => {
    onClose(); // optimistic: the issue flips immediately
    action.mutate(
      { issue, note },
      {
        onSuccess: () => toast.success("Issue resolved"),
        onError: (error) => toast.error(isAppError(error) ? error.userMessage : "Couldn't resolve the issue"),
      },
    );
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
    >
      <DialogContent title="Resolve issue" description={issue.message}>
        <form onSubmit={submit} className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="resolve-note">Why is this acceptable?</Label>
            <Textarea
              id="resolve-note"
              autoFocus
              aria-invalid={errors.note ? true : undefined}
              aria-describedby={errors.note ? "resolve-note-error" : undefined}
              {...register("note")}
            />
            {errors.note ? <FieldMessage id="resolve-note-error">{errors.note.message}</FieldMessage> : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Resolve</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function IssuesPanel({
  issues,
  activeId,
  documentId,
  onActivate,
  onApply,
  canApply,
}: {
  issues: IssueOut[];
  activeId: string | null;
  documentId: string;
  onActivate: (issue: IssueOut) => void;
  onApply: (issue: IssueOut) => void;
  canApply: (issue: IssueOut) => boolean;
}) {
  const [resolving, setResolving] = useState<IssueOut | null>(null);
  const action = useIssueAction(documentId);
  const refs = useRef(new Map<string, HTMLLIElement>());

  useEffect(() => {
    if (activeId) {
      refs.current.get(activeId)?.scrollIntoView({ block: "nearest" });
    }
  }, [activeId]);

  if (issues.length === 0) {
    return <p className="text-muted-foreground text-sm">No issues. This invoice passed every check.</p>;
  }

  return (
    <div className="space-y-4">
      {ORDER.map((severity) => {
        const group = issues.filter((i) => i.severity === severity);
        if (group.length === 0) {
          return null;
        }
        return (
          <section key={severity} aria-label={`${severityLabel(severity)} issues`} className="space-y-2">
            <h4 className="text-sm font-medium">
              <SeverityBadge severity={severity} count={group.length} />
            </h4>
            <ul className="space-y-2">
              {group.map((issue) => (
                <li
                  key={issue.id}
                  ref={(el) => {
                    if (el) {
                      refs.current.set(issue.id, el);
                    } else {
                      refs.current.delete(issue.id);
                    }
                  }}
                  className={cn(
                    "border-border rounded-md border p-3 text-sm",
                    issue.resolved && "opacity-70",
                    activeId === issue.id && "border-primary ring-primary/30 ring-2",
                  )}
                >
                  <button
                    type="button"
                    className="focus-visible:outline-ring block w-full text-left focus-visible:outline-2"
                    aria-current={activeId === issue.id ? "true" : undefined}
                    onClick={() => {
                      onActivate(issue);
                    }}
                  >
                    <span className="text-muted-foreground font-mono text-xs">{issue.rule_code}</span>
                    {issue.field_path ? (
                      <span className="text-muted-foreground ml-2 text-xs">· {issue.field_path}</span>
                    ) : null}
                    <span className="mt-1 block">{issue.message}</span>
                    {issue.expected_value !== null || issue.actual_value !== null ? (
                      <span className="text-muted-foreground mt-1 block text-xs">
                        Expected <q className="font-mono">{issue.expected_value ?? "—"}</q>, found{" "}
                        <q className="font-mono">{issue.actual_value ?? "—"}</q>
                      </span>
                    ) : null}
                  </button>
                  {issue.resolved ? (
                    <p className="mt-2 text-xs">
                      <CheckIcon className="text-success mr-1 inline size-3" aria-hidden="true" />
                      Resolved{issue.resolved_by ? ` by ${issue.resolved_by}` : ""}: {issue.resolution_note}
                    </p>
                  ) : null}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {canApply(issue) ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          onApply(issue);
                        }}
                      >
                        <WandIcon aria-hidden="true" /> Apply suggestion
                      </Button>
                    ) : null}
                    {issue.resolved ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          action.mutate(
                            { issue },
                            {
                              onError: (e) =>
                                toast.error(isAppError(e) ? e.userMessage : "Couldn't reopen the issue"),
                            },
                          );
                        }}
                      >
                        <RotateCcwIcon aria-hidden="true" /> Reopen
                      </Button>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setResolving(issue);
                        }}
                      >
                        <CheckIcon aria-hidden="true" /> Resolve
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
      {resolving ? (
        <ResolveDialog
          issue={resolving}
          documentId={documentId}
          onClose={() => {
            setResolving(null);
          }}
        />
      ) : null}
    </div>
  );
}
