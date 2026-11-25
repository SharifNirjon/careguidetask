"use client";

import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  FileSearchIcon,
  PencilIcon,
  SparklesIcon,
  WandIcon,
} from "lucide-react";
import { useFormContext } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { FieldMessage, Input, Label } from "@/components/ui/form-controls";
import type { FieldOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { AMOUNT_FIELDS, FIELD_LABELS, type HeaderField, type ReviewFormValues } from "../schemas";

const LOW_CONFIDENCE = 0.6;
const SOURCE: Record<string, { label: string; icon: typeof SparklesIcon }> = {
  llm: { label: "Model", icon: SparklesIcon },
  rule: { label: "Rule", icon: WandIcon },
  manual: { label: "Manual", icon: PencilIcon },
};

function Confidence({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return null;
  }
  const low = value < LOW_CONFIDENCE;
  return (
    <span className={cn("text-xs tabular-nums", low ? "text-warning font-medium" : "text-muted-foreground")}>
      {low ? <AlertTriangleIcon className="mr-0.5 inline size-3" aria-hidden="true" /> : null}
      {Math.round(value * 100)}% confidence
    </span>
  );
}

function Grounded({ value }: { value: boolean | null | undefined }) {
  if (value === null || value === undefined) {
    return null;
  }
  return value ? (
    <span className="text-success inline-flex items-center gap-0.5 text-xs">
      <CheckCircle2Icon className="size-3" aria-hidden="true" /> Found in document
    </span>
  ) : (
    <span className="text-destructive inline-flex items-center gap-0.5 text-xs font-medium">
      <AlertTriangleIcon className="size-3" aria-hidden="true" /> Not found in document
    </span>
  );
}

export function FieldRow({
  name,
  field,
  onShowOnPage,
}: {
  name: HeaderField;
  field: FieldOut | undefined;
  onShowOnPage: (unitIndex: number) => void;
}) {
  const {
    register,
    formState: { errors, dirtyFields },
  } = useFormContext<ReviewFormValues>();
  const id = `field-${name}`;
  const error = errors.fields?.[name]?.message;
  const source = field?.source ? SOURCE[field.source] : undefined;
  const SourceIcon = source?.icon;
  // The model proposed a value that grounding rejected: show it, never fill it in.
  const suggestion =
    field && field.value === null && field.grounded === false && field.raw_value ? field.raw_value : null;
  const unitIndex = field?.unit_index ?? null;

  return (
    <div className="space-y-1 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Label htmlFor={id} className="w-32 shrink-0">
          {FIELD_LABELS[name]}
          {dirtyFields.fields?.[name] ? <span className="text-info ml-1 text-xs">(edited)</span> : null}
        </Label>
        <Confidence value={field?.confidence} />
        <Grounded value={field?.grounded} />
        {source && SourceIcon ? (
          <span className="text-muted-foreground inline-flex items-center gap-0.5 text-xs">
            <SourceIcon className="size-3" aria-hidden="true" /> {source.label}
          </span>
        ) : null}
      </div>
      <Input
        id={id}
        inputMode={AMOUNT_FIELDS.has(name) ? "decimal" : undefined}
        placeholder={name === "invoice_date" ? "YYYY-MM-DD" : name === "currency" ? "EUR" : undefined}
        aria-invalid={error ? true : undefined}
        aria-describedby={
          [error ? `${id}-error` : null, field?.evidence ? `${id}-evidence` : null]
            .filter(Boolean)
            .join(" ") || undefined
        }
        {...register(`fields.${name}`)}
      />
      {error ? <FieldMessage id={`${id}-error`}>{error}</FieldMessage> : null}
      {suggestion ? (
        <p className="text-warning text-xs">
          Model suggested <q className="font-mono">{suggestion}</q> — not found in document
        </p>
      ) : null}
      {field?.evidence ? (
        <div className="flex items-start gap-2">
          <p id={`${id}-evidence`} className="text-muted-foreground line-clamp-2 flex-1 text-xs">
            Evidence: <q>{field.evidence}</q>
          </p>
          {unitIndex !== null ? (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-xs"
              onClick={() => {
                onShowOnPage(unitIndex);
              }}
            >
              <FileSearchIcon aria-hidden="true" /> Show on page {unitIndex + 1}
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
