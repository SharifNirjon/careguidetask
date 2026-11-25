"use client";

import { PlayIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { isAppError } from "@/lib/api/errors";
import type { FileKind } from "@/lib/api/types";

import { useStartRun } from "../hooks";
import { MAX_SET_SIZE } from "../schemas";
import { SetPicker } from "./set-picker";

function toggle(set: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  return next;
}

export function ReconciliationView() {
  const [left, setLeft] = useState<ReadonlySet<string>>(new Set());
  const [right, setRight] = useState<ReadonlySet<string>>(new Set());
  const [leftType, setLeftType] = useState<FileKind | undefined>("pdf");
  const [rightType, setRightType] = useState<FileKind | undefined>("xlsx");
  const start = useStartRun();
  const ready = left.size > 0 && right.size > 0 && left.size <= MAX_SET_SIZE && right.size <= MAX_SET_SIZE;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Reconciliation</h1>
        <p className="text-muted-foreground text-sm">
          Check one set of documents against another, for example invoices (PDF) against an accounting export
          (XLSX).
        </p>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <SetPicker
          id="left"
          title="Left set"
          fileType={leftType}
          onFileType={setLeftType}
          selected={left}
          onToggle={(id) => {
            setLeft((s) => toggle(s, id));
          }}
        />
        <SetPicker
          id="right"
          title="Right set"
          fileType={rightType}
          onFileType={setRightType}
          selected={right}
          onToggle={(id) => {
            setRight((s) => toggle(s, id));
          }}
        />
      </div>
      <Button
        disabled={!ready || start.isPending}
        onClick={() => {
          start.mutate(
            { left: [...left], right: [...right] },
            { onError: (e) => toast.error(isAppError(e) ? e.userMessage : "Couldn't start the check") },
          );
        }}
      >
        <PlayIcon aria-hidden="true" /> {start.isPending ? "Starting…" : "Run check"}
      </Button>
      {!ready ? (
        <p className="text-muted-foreground text-sm">Select at least one document on each side.</p>
      ) : null}
    </div>
  );
}
