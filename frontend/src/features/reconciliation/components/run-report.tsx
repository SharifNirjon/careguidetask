"use client";

import { LoaderIcon } from "lucide-react";
import Link from "next/link";

import { ErrorState } from "@/components/ui/error-state";
import { Card, Skeleton } from "@/components/ui/misc";
import { formatTimestamp } from "@/lib/format/dates";
import { formatMoney } from "@/lib/format/money";

import { useRun } from "../hooks";
import { reportSchema, type ReportEntry, type ReportPair } from "../schemas";

function EntryLink({ entry }: { entry: ReportEntry }) {
  return (
    <span>
      <Link
        href={`/documents/${entry.document_id}?extraction=${entry.extraction_id}`}
        className="text-primary hover:underline"
      >
        {entry.invoice_number ?? entry.filename}
      </Link>
      <span className="text-muted-foreground">
        {entry.supplier ? ` · ${entry.supplier}` : ""}
        {entry.total ? ` · ${formatMoney(entry.total)}` : ""}
      </span>
    </span>
  );
}

function show(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Pairs({ pairs, withDiffs }: { pairs: ReportPair[]; withDiffs: boolean }) {
  if (pairs.length === 0) {
    return <p className="text-muted-foreground text-sm">None.</p>;
  }
  return (
    <ul className="space-y-2 text-sm">
      {pairs.map((p) => (
        <li
          key={`${p.left.extraction_id}-${p.right.extraction_id}`}
          className="border-border rounded border p-2"
        >
          <div className="flex flex-wrap gap-2">
            <EntryLink entry={p.left} /> <span aria-hidden="true">↔</span>
            <span className="sr-only">matched with</span> <EntryLink entry={p.right} />
            {p.match === "fuzzy" ? (
              <span className="text-muted-foreground text-xs">(similar, no shared number)</span>
            ) : null}
          </div>
          {withDiffs && p.differences.length > 0 ? (
            <table className="mt-2 w-full text-xs">
              <caption className="sr-only">Field differences</caption>
              <thead className="text-muted-foreground text-left">
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Left</th>
                  <th scope="col">Right</th>
                </tr>
              </thead>
              <tbody>
                {p.differences.map((d) => (
                  <tr key={d.field} className="border-border border-t">
                    <td className="py-1">{d.field}</td>
                    <td className="py-1 font-mono">{show(d.left)}</td>
                    <td className="py-1 font-mono">{show(d.right)}</td>
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

function Entries({ entries }: { entries: ReportEntry[] }) {
  return entries.length === 0 ? (
    <p className="text-muted-foreground text-sm">None.</p>
  ) : (
    <ul className="space-y-1 text-sm">
      {entries.map((e) => (
        <li key={e.extraction_id}>
          <EntryLink entry={e} />
        </li>
      ))}
    </ul>
  );
}

export function RunReport({ runId }: { runId: string }) {
  const { data, error, isPending, refetch } = useRun(runId);
  if (isPending) {
    return <Skeleton className="h-64" />;
  }
  if (error) {
    return <ErrorState error={error} onRetry={() => void refetch()} title="Couldn't load the run" />;
  }
  if (data.status === "running") {
    return (
      <Card role="status" className="flex items-center gap-2">
        <LoaderIcon className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Checking
        the documents…
      </Card>
    );
  }
  if (data.status === "failed") {
    return <ErrorState error={null} title="The check failed" />;
  }
  const parsed = reportSchema.safeParse(data.report);
  if (!parsed.success) {
    return <ErrorState error={null} title="The report has an unexpected format" />;
  }
  const report = parsed.data;
  const sections = [
    { key: "matched", title: "Matched", body: <Pairs pairs={report.matched} withDiffs={false} /> },
    { key: "mismatched", title: "Mismatched", body: <Pairs pairs={report.mismatched} withDiffs /> },
    { key: "only_in_left", title: "Only in left set", body: <Entries entries={report.only_in_left} /> },
    { key: "only_in_right", title: "Only in right set", body: <Entries entries={report.only_in_right} /> },
    {
      key: "ambiguous",
      title: "Ambiguous (more than one candidate)",
      body: <Entries entries={report.ambiguous.flatMap((group) => Object.values(group).flat())} />,
    },
  ] as const;
  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">
        Finished {formatTimestamp(data.finished_at)} · {data.left_document_ids.length} left,{" "}
        {data.right_document_ids.length} right documents
      </p>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {sections.map((s) => (
          <div key={s.key} className="border-border rounded-lg border p-3">
            <dt className="text-muted-foreground text-xs">{s.title}</dt>
            <dd className="text-2xl font-semibold tabular-nums">{report.counts[s.key]}</dd>
          </div>
        ))}
      </dl>
      {sections.map((s) => (
        <Card key={s.key} aria-labelledby={`h-${s.key}`}>
          <h2 id={`h-${s.key}`} className="mb-2 font-medium">
            {s.title} ({report.counts[s.key]})
          </h2>
          {s.body}
        </Card>
      ))}
    </div>
  );
}
