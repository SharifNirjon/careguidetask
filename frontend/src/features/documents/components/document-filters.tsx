"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Label, NativeSelect } from "@/components/ui/form-controls";

import { DOCUMENT_STATUSES, FILE_TYPES, REVIEW_STATUSES, type DocumentFilters } from "../schemas";

const TEXT_DEBOUNCE_MS = 400;

const LABELS: Record<string, string> = {
  uploaded: "Uploaded",
  queued: "Queued",
  processing: "Processing",
  completed: "Completed",
  needs_review: "Needs review",
  failed: "Failed",
  pending: "Pending",
  auto_approved: "Auto-approved",
  approved: "Approved",
  rejected: "Rejected",
};

/** A text filter that commits to the URL after a pause (not on every keystroke). */
function DebouncedInput({
  id,
  label,
  value,
  onCommit,
  placeholder,
  inputMode,
}: {
  id: string;
  label: string;
  value: string | undefined;
  onCommit: (value: string | undefined) => void;
  placeholder?: string;
  inputMode?: "decimal" | "text";
}) {
  const [draft, setDraft] = useState(value ?? "");
  // The URL changed from outside (back button, Clear filters): show the new value.
  const [shown, setShown] = useState(value);
  if (shown !== value) {
    setShown(value);
    setDraft(value ?? "");
  }
  useEffect(() => {
    if (draft === (value ?? "")) {
      return;
    }
    const timer = setTimeout(() => {
      onCommit(draft.trim() || undefined);
    }, TEXT_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [draft, value, onCommit]);
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={draft}
        placeholder={placeholder}
        inputMode={inputMode}
        onChange={(e) => {
          setDraft(e.target.value);
        }}
      />
    </div>
  );
}

function Select<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: T | undefined;
  options: readonly T[];
  onChange: (value: T | undefined) => void;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <NativeSelect
        id={id}
        value={value ?? ""}
        onChange={(e) => {
          onChange(options.find((o) => o === e.target.value) ?? undefined);
        }}
      >
        <option value="">Any</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {LABELS[o] ?? o.toUpperCase()}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

export function DocumentFiltersPanel({
  filters,
  onChange,
  idPrefix = "f",
}: {
  filters: DocumentFilters;
  onChange: (next: Partial<DocumentFilters>) => void;
  idPrefix?: string;
}) {
  const id = (name: string) => `${idPrefix}-${name}`;
  return (
    <fieldset className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
      <legend className="sr-only">Filters</legend>
      <Select
        id={id("status")}
        label="Status"
        value={filters.status}
        options={DOCUMENT_STATUSES}
        onChange={(status) => {
          onChange({ status });
        }}
      />
      <Select
        id={id("review")}
        label="Review status"
        value={filters.review_status}
        options={REVIEW_STATUSES}
        onChange={(review_status) => {
          onChange({ review_status });
        }}
      />
      <Select
        id={id("type")}
        label="File type"
        value={filters.file_type}
        options={FILE_TYPES}
        onChange={(file_type) => {
          onChange({ file_type });
        }}
      />
      <Select
        id={id("issues")}
        label="Open issues"
        value={filters.has_open_issues}
        options={["true", "false"] as const}
        onChange={(has_open_issues) => {
          onChange({ has_open_issues });
        }}
      />
      <DebouncedInput
        id={id("supplier")}
        label="Supplier"
        value={filters.supplier}
        onCommit={(supplier) => {
          onChange({ supplier });
        }}
      />
      <DebouncedInput
        id={id("customer")}
        label="Customer"
        value={filters.customer}
        onCommit={(customer) => {
          onChange({ customer });
        }}
      />
      <DebouncedInput
        id={id("number")}
        label="Invoice number"
        value={filters.invoice_number}
        onCommit={(invoice_number) => {
          onChange({ invoice_number });
        }}
      />
      <DebouncedInput
        id={id("currency")}
        label="Currency"
        placeholder="EUR"
        value={filters.currency}
        onCommit={(currency) => {
          onChange({ currency: currency?.toUpperCase() });
        }}
      />
      <div className="space-y-1">
        <Label htmlFor={id("from")}>Invoice date from</Label>
        <Input
          id={id("from")}
          type="date"
          value={filters.invoice_date_from ?? ""}
          onChange={(e) => {
            onChange({ invoice_date_from: e.target.value || undefined });
          }}
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor={id("to")}>Invoice date to</Label>
        <Input
          id={id("to")}
          type="date"
          value={filters.invoice_date_to ?? ""}
          onChange={(e) => {
            onChange({ invoice_date_to: e.target.value || undefined });
          }}
        />
      </div>
      <DebouncedInput
        id={id("min")}
        label="Total min"
        inputMode="decimal"
        value={filters.total_min}
        onCommit={(total_min) => {
          onChange({ total_min });
        }}
      />
      <DebouncedInput
        id={id("max")}
        label="Total max"
        inputMode="decimal"
        value={filters.total_max}
        onCommit={(total_max) => {
          onChange({ total_max });
        }}
      />
    </fieldset>
  );
}

export function ClearFiltersButton({ onClear }: { onClear: () => void }) {
  return (
    <Button variant="link" size="sm" onClick={onClear}>
      Clear filters
    </Button>
  );
}
