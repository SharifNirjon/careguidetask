"use client";

import Link from "next/link";

import { ReviewStatusBadge } from "@/components/status-badges";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/misc";
import { useEvents } from "@/features/documents/hooks";
import type { ExtractionOut, ReconciliationOut } from "@/lib/api/types";
import { formatDuration, formatTimestamp } from "@/lib/format/dates";

import { useCorrections, useExtraction, useVersions } from "../hooks";

function renderValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

function VersionsAndCorrections({ extractionId }: { extractionId: string }) {
  const versions = useVersions(extractionId);
  const corrections = useCorrections(extractionId);
  if (versions.isPending || corrections.isPending) {
    return <Skeleton className="h-24" />;
  }
  if (versions.error || corrections.error) {
    return (
      <ErrorState
        error={versions.error ?? corrections.error}
        onRetry={() => void (versions.refetch(), corrections.refetch())}
      />
    );
  }
  return (
    <div className="space-y-4 text-sm">
      <section>
        <h4 className="mb-1 font-medium">Versions</h4>
        <ol className="space-y-1">
          {versions.data.map((v) => (
            <li key={v.id} className="flex flex-wrap items-center gap-3">
              <span className="font-mono">v{v.version}</span>
              {v.is_current ? <span className="text-xs font-medium">current</span> : null}
              <ReviewStatusBadge status={v.review_status} />
              <span className="text-muted-foreground">{v.model_name ?? "manual/rule"}</span>
              <span className="text-muted-foreground">{formatTimestamp(v.created_at)}</span>
            </li>
          ))}
        </ol>
      </section>
      <section>
        <h4 className="mb-1 font-medium">Corrections</h4>
        {corrections.data.length === 0 ? (
          <p className="text-muted-foreground">No manual corrections yet.</p>
        ) : (
          <table className="w-full text-xs">
            <caption className="sr-only">Correction audit trail</caption>
            <thead className="text-muted-foreground text-left">
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Old</th>
                <th scope="col">New</th>
                <th scope="col">By</th>
                <th scope="col">When</th>
              </tr>
            </thead>
            <tbody>
              {corrections.data.map((c) => (
                <tr key={c.id} className="border-border border-t align-top">
                  <td className="py-1 font-mono">{c.field_path}</td>
                  <td className="py-1">{renderValue(c.old_value)}</td>
                  <td className="py-1">{renderValue(c.new_value)}</td>
                  <td className="py-1">{c.corrected_by}</td>
                  <td className="py-1 whitespace-nowrap">{formatTimestamp(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function CounterpartLink({ extractionId }: { extractionId: string }) {
  const { data } = useExtraction(extractionId);
  if (!data) {
    return <span className="text-muted-foreground">Loading…</span>;
  }
  const label = `${data.fields.invoice_number?.value ?? "Invoice"} · ${data.fields.supplier_name?.value ?? ""}`;
  return (
    <Link href={`/documents/${data.document_id}`} className="text-primary hover:underline">
      {label}
    </Link>
  );
}

function Reconciliations({ items }: { items: ReconciliationOut[] }) {
  if (items.length === 0) {
    return <p className="text-muted-foreground text-sm">No related documents found.</p>;
  }
  return (
    <ul className="space-y-3 text-sm">
      {items.map((r) => (
        <li key={r.id} className="border-border rounded border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={r.status === "matched" ? "text-success font-medium" : "text-destructive font-medium"}
            >
              {r.status === "matched" ? "Matched" : "Mismatched"}
            </span>
            <span className="text-muted-foreground">
              by {r.match_method === "fuzzy" ? "similar fields" : "invoice number"} with
            </span>
            <CounterpartLink extractionId={r.counterpart_extraction_id} />
          </div>
          {r.differences.length > 0 ? (
            <table className="mt-2 w-full text-xs">
              <caption className="sr-only">Differences</caption>
              <thead className="text-muted-foreground text-left">
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">This invoice</th>
                  <th scope="col">Other</th>
                </tr>
              </thead>
              <tbody>
                {r.differences.map((d, i) => (
                  <tr key={i} className="border-border border-t">
                    <td className="py-1">{renderValue(d.field)}</td>
                    <td className="py-1 font-mono">{renderValue(d.left)}</td>
                    <td className="py-1 font-mono">{renderValue(d.right)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function Timeline({ documentId }: { documentId: string }) {
  const { data, error, isPending, refetch } = useEvents(documentId);
  if (isPending) {
    return <Skeleton className="h-24" />;
  }
  if (error) {
    return <ErrorState error={error} onRetry={() => void refetch()} />;
  }
  return (
    <ol className="space-y-1 text-sm">
      {data.map((e) => (
        <li key={e.id} className="flex flex-wrap gap-3">
          <span className="w-20 font-medium">{e.stage}</span>
          <span className={e.status === "failed" ? "text-destructive" : ""}>{e.status}</span>
          <span className="text-muted-foreground tabular-nums">{formatDuration(e.duration_ms)}</span>
          <span className="text-muted-foreground">{formatTimestamp(e.created_at)}</span>
        </li>
      ))}
    </ol>
  );
}

export function ReviewTabs({ extraction, documentId }: { extraction: ExtractionOut; documentId: string }) {
  return (
    <Tabs defaultValue="history" className="space-y-3">
      <TabsList>
        <TabsTrigger value="history">Versions &amp; corrections</TabsTrigger>
        <TabsTrigger value="reconciliations">
          Reconciliations ({extraction.reconciliations.length})
        </TabsTrigger>
        <TabsTrigger value="timeline">Processing timeline</TabsTrigger>
      </TabsList>
      <TabsContent value="history">
        <VersionsAndCorrections extractionId={extraction.id} />
      </TabsContent>
      <TabsContent value="reconciliations">
        <Reconciliations items={extraction.reconciliations} />
      </TabsContent>
      <TabsContent value="timeline">
        <Timeline documentId={documentId} />
      </TabsContent>
    </Tabs>
  );
}
