"use client";

import dynamic from "next/dynamic";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useState } from "react";

import { DocumentStatusBadge, TERMINAL_STATUSES } from "@/components/status-badges";
import { ErrorState } from "@/components/ui/error-state";
import { Card, Skeleton } from "@/components/ui/misc";
import { useDocument, useResults } from "@/features/documents/hooks";
import { messageFor } from "@/lib/api/errors";
import type { DocumentDetail, ExtractionOut } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { ReviewPanel } from "./review-panel";

// The viewer (images, zoom, sheet tables) loads on demand: it isn't needed for first paint.
const DocumentViewer = dynamic(() => import("./document-viewer").then((m) => m.DocumentViewer), {
  ssr: false,
  loading: () => <Skeleton className="h-full min-h-96 w-full" />,
});

function intParam(value: string | null, fallback: number): number {
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 ? n : fallback;
}

function InvoiceSwitcher({
  extractions,
  current,
  onSelect,
}: {
  extractions: ExtractionOut[];
  current: number;
  onSelect: (index: number) => void;
}) {
  if (extractions.length < 2) {
    return null;
  }
  return (
    <nav aria-label="Invoices in this document" className="flex flex-wrap gap-2">
      {extractions.map((e, i) => (
        <button
          key={e.id}
          type="button"
          aria-current={i === current ? "true" : undefined}
          onClick={() => {
            onSelect(i);
          }}
          className={cn(
            "focus-visible:outline-ring rounded-md border px-3 py-1 text-sm focus-visible:outline-2",
            i === current ? "border-primary bg-primary/10 font-medium" : "border-border hover:bg-muted",
          )}
        >
          Invoice {i + 1}{" "}
          <span className="text-muted-foreground">
            (
            {e.unit_start === e.unit_end
              ? `page ${e.unit_start + 1}`
              : `pages ${e.unit_start + 1}–${e.unit_end + 1}`}
            )
          </span>
        </button>
      ))}
    </nav>
  );
}

function Processing({ document }: { document: DocumentDetail }) {
  const { progress } = document;
  return (
    <Card className="space-y-3" aria-live="polite">
      <DocumentStatusBadge status={document.status} />
      <p className="text-sm">
        {progress.stage ? `Stage: ${progress.stage}` : "Waiting for a worker"} · {progress.percent}%
      </p>
      <progress className="w-full" max={100} value={progress.percent} aria-label="Processing progress" />
      {document.error ? (
        <p className="text-warning text-sm">{messageFor(document.error.code, document.error.message)}</p>
      ) : null}
    </Card>
  );
}

export function ReviewScreen({ documentId }: { documentId: string }) {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const detail = useDocument(documentId);
  const finished = detail.data ? TERMINAL_STATUSES.has(detail.data.status) : false;
  const results = useResults(documentId, finished);
  const [evidence, setEvidence] = useState<string | null>(null);

  const invoice = intParam(search.get("invoice"), 0);
  const page = intParam(search.get("page"), 0);

  const setParams = useCallback(
    (next: { invoice?: number; page?: number }) => {
      const params = new URLSearchParams(search.toString());
      if (next.invoice !== undefined) {
        params.delete("extraction"); // an explicit invoice choice wins over the search link
      }
      for (const [key, value] of Object.entries(next)) {
        params.set(key, String(value));
      }
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [pathname, router, search],
  );

  if (detail.isPending) {
    return <Skeleton className="h-[70vh] w-full" />;
  }
  if (detail.error) {
    return (
      <ErrorState
        error={detail.error}
        onRetry={() => void detail.refetch()}
        title="Couldn't load this document"
      />
    );
  }
  const document = detail.data;
  const extractions = [...(results.data?.extractions ?? [])].sort(
    (a, b) => a.invoice_index - b.invoice_index,
  );
  // A search result links with ?extraction=<id>: open that invoice; otherwise ?invoice=<n>.
  const linked = extractions.findIndex((e) => e.id === search.get("extraction"));
  const extraction =
    extractions[linked >= 0 ? linked : Math.min(invoice, Math.max(extractions.length - 1, 0))];

  return (
    <div className="grid h-[calc(100vh-7rem)] grid-cols-1 gap-4 xl:grid-cols-2">
      <div className="min-h-0">
        <DocumentViewer
          documentId={document.id}
          fileType={document.file_type}
          filename={document.filename}
          unitCount={document.unit_count || document.page_count || 1}
          page={page}
          onPageChange={(p) => {
            setParams({ page: p });
          }}
          evidence={evidence}
        />
      </div>
      <div className="min-h-0 space-y-4 overflow-y-auto pr-1">
        {!finished ? (
          <>
            <h1 className="text-xl font-semibold">{document.filename}</h1>
            <Processing document={document} />
          </>
        ) : results.isPending ? (
          <Skeleton className="h-96 w-full" />
        ) : results.error ? (
          <ErrorState
            error={results.error}
            onRetry={() => void results.refetch()}
            title="Couldn't load the results"
          />
        ) : !extraction ? (
          <Card className="space-y-2">
            <h1 className="text-xl font-semibold">{document.filename}</h1>
            <DocumentStatusBadge status={document.status} />
            <p className="text-sm">
              {document.error
                ? messageFor(document.error.code, document.error.message)
                : "No invoice was found in this document."}
            </p>
          </Card>
        ) : (
          <>
            <InvoiceSwitcher
              extractions={extractions}
              current={extractions.indexOf(extraction)}
              onSelect={(i) => {
                const target = extractions[i];
                if (target) {
                  setParams({ invoice: i, page: target.unit_start });
                }
              }}
            />
            <ReviewPanel
              key={extraction.id}
              document={document}
              extraction={extraction}
              onShowOnPage={(unit, ev) => {
                setEvidence(ev);
                setParams({ page: unit });
              }}
              onRefetchResults={() => results.refetch()}
            />
          </>
        )}
      </div>
    </div>
  );
}
