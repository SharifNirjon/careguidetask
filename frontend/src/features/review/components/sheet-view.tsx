"use client";

import { useEffect, useRef } from "react";

import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

import { useUnitTable } from "../hooks";

/** Spreadsheet evidence is "sheet <name> row <n> col <header>" (set by the tabular extractor). */
export function parseCellEvidence(evidence: string | null): { row: number; column: string } | null {
  const match = evidence ? /row (\d+) col (.+)$/.exec(evidence) : null;
  return match?.[1] && match[2] ? { row: Number(match[1]), column: match[2].trim() } : null;
}

/** The parsed rows of a sheet / CSV, with the cell a field came from highlighted. */
export function SheetView({
  documentId,
  unitIndex,
  evidence,
}: {
  documentId: string;
  unitIndex: number;
  evidence: string | null;
}) {
  const { data, error, isPending, refetch } = useUnitTable(documentId, unitIndex, true);
  const target = parseCellEvidence(evidence);
  const highlighted = useRef<HTMLTableCellElement | null>(null);

  useEffect(() => {
    highlighted.current?.scrollIntoView({ block: "center", inline: "center" });
  }, [data, evidence]);

  if (isPending) {
    return <Skeleton className="h-64 w-full" />;
  }
  if (error) {
    return <ErrorState error={error} onRetry={() => void refetch()} title="Couldn't load the sheet" />;
  }
  return (
    <div className="bg-background space-y-4 p-2">
      {data.sheet_name ? <h3 className="font-medium">Sheet: {data.sheet_name}</h3> : null}
      {data.blocks.map((block, b) => (
        <table key={b} className="w-full border-collapse text-xs">
          <caption className="sr-only">Table {b + 1}</caption>
          <thead>
            <tr>
              <th scope="col" className="border-border bg-muted border px-2 py-1 text-left">
                Row
              </th>
              {block.headers.map((h) => (
                <th key={h} scope="col" className="border-border bg-muted border px-2 py-1 text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row) => (
              <tr key={row.row_number}>
                <th scope="row" className="border-border text-muted-foreground border px-2 py-1 text-left">
                  {row.row_number}
                </th>
                {row.cells.map((cell, c) => {
                  const hit = target?.row === row.row_number && target.column === block.headers[c];
                  return (
                    <td
                      key={c}
                      ref={hit ? highlighted : undefined}
                      aria-label={hit ? `${cell} (evidence for the selected field)` : undefined}
                      className={cn(
                        "border-border border px-2 py-1",
                        hit && "bg-highlight outline-warning font-semibold outline-2",
                      )}
                    >
                      {cell}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      ))}
      {data.truncated ? (
        <p className="text-muted-foreground text-xs">Only the first rows are shown.</p>
      ) : null}
    </div>
  );
}
