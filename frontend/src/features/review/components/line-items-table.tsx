"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertTriangleIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { useRef } from "react";
import { useFieldArray, useFormContext, useWatch } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form-controls";

import { lineHint, sumHint, type LineField, type ReviewFormValues } from "../schemas";

const VIRTUALIZE_OVER = 200;
const ROW_PX = 44;
const COLUMNS: ReadonlyArray<{ field: LineField; label: string; width: string }> = [
  { field: "description", label: "Description", width: "min-w-48" },
  { field: "quantity", label: "Qty", width: "w-20" },
  { field: "unit_price", label: "Unit price", width: "w-28" },
  { field: "amount", label: "Amount", width: "w-28" },
  { field: "tax_rate", label: "Tax rate", width: "w-20" },
];

function Row({ index, onRemove }: { index: number; onRemove: () => void }) {
  const { register, control, formState } = useFormContext<ReviewFormValues>();
  const row = useWatch({ control, name: `lineItems.${index}` });
  const hint = lineHint(row);
  const errors = formState.errors.lineItems?.[index];
  return (
    <tr className="border-border border-b align-top">
      <td className="text-muted-foreground px-1 py-1 text-xs tabular-nums">{index + 1}</td>
      {COLUMNS.map(({ field, label, width }) => (
        <td key={field} className={`px-1 py-1 ${width}`}>
          <Input
            aria-label={`Line ${index + 1} ${label}`}
            inputMode={field === "description" ? undefined : "decimal"}
            aria-invalid={errors?.[field] ? true : undefined}
            className="h-8"
            {...register(`lineItems.${index}.${field}`)}
          />
          {errors?.[field]?.message ? (
            <p className="text-destructive text-xs">{errors[field].message}</p>
          ) : null}
          {field === "amount" && hint ? (
            <p className="text-warning text-xs" role="note">
              <AlertTriangleIcon className="inline size-3" aria-hidden="true" /> {hint}
            </p>
          ) : null}
        </td>
      ))}
      <td className="px-1 py-1">
        <Button variant="ghost" size="icon" aria-label={`Delete line ${index + 1}`} onClick={onRemove}>
          <Trash2Icon />
        </Button>
      </td>
    </tr>
  );
}

/**
 * Editable line items. Arithmetic hints (qty x price vs amount, lines vs
 * subtotal) are UX only; the server validates. Over 200 rows only the visible
 * rows are rendered (form state keeps every row's values).
 */
export function LineItemsTable() {
  const { control } = useFormContext<ReviewFormValues>();
  const { fields, append, remove } = useFieldArray({ control, name: "lineItems", keyName: "_key" });
  const values = useWatch({ control }) as ReviewFormValues;
  const total = sumHint(values);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const virtual = fields.length > VIRTUALIZE_OVER;
  // The app doesn't use the React Compiler; TanStack Virtual's functions are only read during this render.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: fields.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_PX,
    enabled: virtual,
    overscan: 10,
  });
  const items = virtual ? virtualizer.getVirtualItems() : fields.map((_, index) => ({ index, start: 0 }));
  const padTop = virtual ? (items[0]?.start ?? 0) : 0;
  const padBottom = virtual ? virtualizer.getTotalSize() - ((items.at(-1)?.start ?? 0) + ROW_PX) : 0;

  return (
    <section aria-labelledby="line-items-heading" className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 id="line-items-heading" className="font-medium">
          Line items <span className="text-muted-foreground text-sm">({fields.length})</span>
        </h3>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            append({
              id: null,
              position: null,
              description: "",
              quantity: "",
              unit_price: "",
              amount: "",
              tax_rate: "",
            });
          }}
        >
          <PlusIcon aria-hidden="true" /> Add line
        </Button>
      </div>
      {total ? (
        <p className="text-warning text-sm" role="note">
          <AlertTriangleIcon className="mr-1 inline size-4" aria-hidden="true" />
          {total} (hint only — the server checks totals when you save)
        </p>
      ) : null}
      <div ref={scrollRef} className={virtual ? "max-h-[480px] overflow-y-auto" : "overflow-x-auto"}>
        <table className="w-full text-sm">
          <caption className="sr-only">Line items</caption>
          <thead>
            <tr className="text-muted-foreground text-left text-xs">
              <th scope="col" className="px-1">
                #
              </th>
              {COLUMNS.map((c) => (
                <th key={c.field} scope="col" className="px-1 font-medium">
                  {c.label}
                </th>
              ))}
              <th scope="col" className="px-1">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {padTop > 0 ? (
              <tr aria-hidden="true">
                <td style={{ height: padTop }} />
              </tr>
            ) : null}
            {items.map(({ index }) => {
              const field = fields[index];
              return field ? (
                <Row
                  key={field._key}
                  index={index}
                  onRemove={() => {
                    remove(index);
                  }}
                />
              ) : null;
            })}
            {padBottom > 0 ? (
              <tr aria-hidden="true">
                <td style={{ height: padBottom }} />
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {fields.length === 0 ? <p className="text-muted-foreground text-sm">No line items.</p> : null}
    </section>
  );
}
