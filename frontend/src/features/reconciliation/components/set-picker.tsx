"use client";

import { useQuery } from "@tanstack/react-query";

import { ErrorState } from "@/components/ui/error-state";
import { Label, NativeSelect } from "@/components/ui/form-controls";
import { Skeleton } from "@/components/ui/misc";
import { documentKeys, fetchDocuments } from "@/features/documents/api";
import { FILE_TYPES, listParamsSchema } from "@/features/documents/schemas";
import type { FileKind } from "@/lib/api/types";

/** One side of a reconciliation: pick documents (filtered by file type) with checkboxes. */
export function SetPicker({
  id,
  title,
  fileType,
  onFileType,
  selected,
  onToggle,
}: {
  id: string;
  title: string;
  fileType: FileKind | undefined;
  onFileType: (type: FileKind | undefined) => void;
  selected: ReadonlySet<string>;
  onToggle: (documentId: string) => void;
}) {
  const params = listParamsSchema.parse({ file_type: fileType });
  const { data, error, isPending, refetch } = useQuery({
    queryKey: documentKeys.list(params),
    queryFn: () => fetchDocuments(params),
  });
  return (
    <fieldset className="border-border space-y-3 rounded-lg border p-4">
      <legend className="px-1 font-medium">
        {title} <span className="text-muted-foreground text-sm">({selected.size} selected)</span>
      </legend>
      <div className="space-y-1">
        <Label htmlFor={`${id}-type`}>File type</Label>
        <NativeSelect
          id={`${id}-type`}
          value={fileType ?? ""}
          onChange={(e) => {
            onFileType(FILE_TYPES.find((t) => t === e.target.value));
          }}
        >
          <option value="">Any</option>
          {FILE_TYPES.map((t) => (
            <option key={t} value={t}>
              {t.toUpperCase()}
            </option>
          ))}
        </NativeSelect>
      </div>
      {isPending ? (
        <Skeleton className="h-32" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : data.items.length === 0 ? (
        <p className="text-muted-foreground text-sm">No documents of this type.</p>
      ) : (
        <ul className="max-h-80 space-y-1 overflow-y-auto">
          {data.items.map((doc) => {
            const checkboxId = `${id}-${doc.id}`;
            const first = doc.invoices[0];
            return (
              <li key={doc.id} className="flex items-start gap-2 text-sm">
                <input
                  id={checkboxId}
                  type="checkbox"
                  className="mt-1 size-4"
                  checked={selected.has(doc.id)}
                  onChange={() => {
                    onToggle(doc.id);
                  }}
                />
                <label htmlFor={checkboxId} className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{doc.filename}</span>
                  <span className="text-muted-foreground block text-xs">
                    {doc.invoices.length} invoice{doc.invoices.length === 1 ? "" : "s"}
                    {first?.supplier_name ? ` · ${first.supplier_name}` : ""}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </fieldset>
  );
}
