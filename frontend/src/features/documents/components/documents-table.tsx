"use client";

import Link from "next/link";

import { DocumentStatusBadge, ReviewStatusBadge, SeverityBadge } from "@/components/status-badges";
import { Tip } from "@/components/ui/misc";
import type { DocumentSummary, InvoiceHeadline } from "@/lib/api/types";
import { formatInvoiceDate, formatTimestamp } from "@/lib/format/dates";
import { formatMoney } from "@/lib/format/money";

const COLUMNS = [
  "File",
  "Type",
  "Status",
  "Review",
  "Supplier",
  "Invoice no.",
  "Invoice date",
  "Total",
  "Open issues",
  "Uploaded",
] as const;

function OpenIssues({ invoices }: { invoices: InvoiceHeadline[] }) {
  const totals = invoices.reduce(
    (acc, i) => ({
      error: acc.error + i.open_issues.error,
      warning: acc.warning + i.open_issues.warning,
      info: acc.info + i.open_issues.info,
    }),
    { error: 0, warning: 0, info: 0 },
  );
  if (!totals.error && !totals.warning && !totals.info) {
    return <span className="text-muted-foreground">None</span>;
  }
  return (
    <span className="flex flex-col gap-0.5">
      {totals.error ? <SeverityBadge severity="error" count={totals.error} /> : null}
      {totals.warning ? <SeverityBadge severity="warning" count={totals.warning} /> : null}
      {totals.info ? <SeverityBadge severity="info" count={totals.info} /> : null}
    </span>
  );
}

function InvoiceDate({ iso }: { iso: string | null }) {
  if (!iso) {
    return <span className="text-muted-foreground">—</span>;
  }
  // The ISO value is always one hover/focus away: no DD/MM vs MM/DD confusion.
  return (
    <Tip content={iso}>
      <time dateTime={iso} tabIndex={0} className="underline decoration-dotted underline-offset-2">
        {formatInvoiceDate(iso)}
      </time>
    </Tip>
  );
}

export function DocumentRow({ doc }: { doc: DocumentSummary }) {
  const [first] = doc.invoices;
  const more = doc.invoices.length - 1;
  return (
    <tr className="border-border hover:bg-muted/50 border-b">
      <th scope="row" className="max-w-64 truncate px-3 py-2 text-left font-medium">
        <Link
          href={`/documents/${doc.id}`}
          className="text-primary focus-visible:outline-ring hover:underline focus-visible:outline-2"
        >
          {doc.filename}
        </Link>
        {more > 0 ? <span className="text-muted-foreground ml-2 text-xs">+{more} invoices</span> : null}
      </th>
      <td className="px-3 py-2 uppercase">{doc.file_type}</td>
      <td className="px-3 py-2">
        <DocumentStatusBadge status={doc.status} />
      </td>
      <td className="px-3 py-2">{first ? <ReviewStatusBadge status={first.review_status} /> : "—"}</td>
      <td className="max-w-48 truncate px-3 py-2">{first?.supplier_name ?? "—"}</td>
      <td className="px-3 py-2 font-mono text-xs">{first?.invoice_number ?? "—"}</td>
      <td className="px-3 py-2">
        <InvoiceDate iso={first?.invoice_date ?? null} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{formatMoney(first?.total, first?.currency)}</td>
      <td className="px-3 py-2">
        <OpenIssues invoices={doc.invoices} />
      </td>
      <td className="text-muted-foreground px-3 py-2 whitespace-nowrap">{formatTimestamp(doc.created_at)}</td>
    </tr>
  );
}

export function DocumentsTable({ documents, caption }: { documents: DocumentSummary[]; caption: string }) {
  return (
    <div className="border-border overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-muted text-left">
          <tr>
            {COLUMNS.map((c) => (
              <th
                key={c}
                scope="col"
                className={`px-3 py-2 font-medium ${c === "Total" ? "text-right" : ""}`}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {documents.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DocumentsTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading documents">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="bg-muted h-10 animate-pulse rounded" />
      ))}
    </div>
  );
}
