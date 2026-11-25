"use client";

import { AlertTriangleIcon, SearchIcon, SlidersHorizontalIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ReviewStatusBadge } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/error-state";
import { Input, Label } from "@/components/ui/form-controls";
import { Skeleton } from "@/components/ui/misc";
import { DocumentFiltersPanel } from "@/features/documents/components/document-filters";
import { compact, parseFilters, type DocumentFilters } from "@/features/documents/schemas";
import type { SearchHit, SearchMode } from "@/lib/api/types";
import { formatInvoiceDate } from "@/lib/format/dates";
import { formatMoney } from "@/lib/format/money";
import { cn } from "@/lib/utils";

import { useCommittedQuery, useSearch } from "../hooks";
import {
  canSubmit,
  MAX_QUERY_CHARS,
  SEARCH_MODES,
  SEARCH_PAGE_SIZE,
  searchParamsSchema,
  SEMANTIC_MIN_CHARS,
} from "../schemas";
import { Snippet } from "./snippet";

const MODE_LABELS: Record<SearchMode, string> = {
  keyword: "Keyword",
  semantic: "Semantic",
  hybrid: "Hybrid",
};
const MATCH_LABELS = {
  keyword: "Keyword match",
  semantic: "Similar meaning",
  both: "Keyword + meaning",
} as const;

function Result({ hit }: { hit: SearchHit }) {
  const params = new URLSearchParams();
  if (hit.extraction_id) {
    params.set("extraction", hit.extraction_id);
  }
  if (hit.unit_index !== null) {
    params.set("page", String(hit.unit_index));
  }
  return (
    <li className="border-border hover:bg-muted/40 rounded-lg border p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <Link
          href={`/documents/${hit.document_id}?${params.toString()}`}
          className="text-primary focus-visible:outline-ring font-medium hover:underline focus-visible:outline-2"
        >
          {hit.invoice_number ? `Invoice ${hit.invoice_number}` : hit.filename}
        </Link>
        {hit.supplier_name ? <span className="text-sm">{hit.supplier_name}</span> : null}
        {hit.invoice_date ? (
          <span className="text-muted-foreground text-sm">{formatInvoiceDate(hit.invoice_date)}</span>
        ) : null}
        {hit.total ? (
          <span className="text-sm tabular-nums">{formatMoney(hit.total, hit.currency)}</span>
        ) : null}
        {hit.review_status ? <ReviewStatusBadge status={hit.review_status} /> : null}
        <span className="text-muted-foreground ml-auto text-xs">
          {MATCH_LABELS[hit.match_type]} · score {hit.score.toFixed(2)}
        </span>
      </div>
      <p className="text-muted-foreground mt-0.5 text-xs">
        {hit.filename}
        {hit.unit_index !== null ? ` · page ${hit.unit_index + 1}` : ""}
      </p>
      <div className="mt-2">
        <Snippet segments={hit.snippet_segments} />
      </div>
    </li>
  );
}

export function SearchView() {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const urlState = useMemo(() => searchParamsSchema.parse(Object.fromEntries(search.entries())), [search]);
  const filters = useMemo(() => parseFilters(new URLSearchParams(search.toString())), [search]);
  const [input, setInput] = useState(urlState.q ?? "");
  const [mode, setMode] = useState<SearchMode>(urlState.mode);
  const [showFilters, setShowFilters] = useState(false);
  const { committed, submit } = useCommittedQuery(input, mode, urlState.q ?? "");
  const offset = committed === (urlState.q ?? "") ? urlState.offset : 0;

  // Keep the URL in sync with what actually ran (shareable, back-button safe).
  const writeUrl = (next: { q?: string; mode?: SearchMode; offset?: number; filters?: DocumentFilters }) => {
    const merged = compact({
      ...filters,
      ...(next.filters ?? {}),
      q: next.q ?? committed,
      mode: next.mode ?? mode,
      offset: next.offset ?? 0,
    });
    const params = new URLSearchParams(Object.entries(merged).map(([k, v]) => [k, String(v)]));
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };

  useEffect(() => {
    if (committed !== (urlState.q ?? "") || mode !== urlState.mode) {
      writeUrl({ q: committed, mode, offset: 0 });
    }
    // writeUrl reads the latest values; only these changes should sync the URL
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [committed, mode]);

  const { data, error, isFetching, refetch } = useSearch({ q: committed, mode, offset, filters });
  const tooShort = mode !== "keyword" && input.trim().length > 0 && input.trim().length < SEMANTIC_MIN_CHARS;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Search</h1>
      <form
        role="search"
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className="min-w-64 flex-1 space-y-1">
          <Label htmlFor="q">Search invoices</Label>
          <div className="relative">
            <SearchIcon
              className="text-muted-foreground absolute top-2.5 left-2.5 size-4"
              aria-hidden="true"
            />
            <Input
              id="q"
              type="search"
              className="pl-8"
              maxLength={MAX_QUERY_CHARS}
              value={input}
              placeholder='e.g. acme invoices in march, "INV-2026-001", -credit'
              aria-describedby="q-hint"
              onChange={(e) => {
                setInput(e.target.value);
              }}
            />
          </div>
          <p id="q-hint" className="text-muted-foreground text-xs">
            {mode === "keyword"
              ? "Searches as you type."
              : `Press Enter to search (or pause). ${MODE_LABELS[mode]} search needs at least ${SEMANTIC_MIN_CHARS} characters.`}
          </p>
        </div>
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium">Mode</legend>
          <div
            className="border-border inline-flex rounded-md border p-0.5"
            role="radiogroup"
            aria-label="Search mode"
          >
            {SEARCH_MODES.map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                onClick={() => {
                  setMode(m);
                }}
                className={cn(
                  "focus-visible:outline-ring rounded px-3 py-1 text-sm focus-visible:outline-2",
                  mode === m ? "bg-primary text-primary-foreground" : "hover:bg-muted",
                )}
              >
                {MODE_LABELS[m]}
              </button>
            ))}
          </div>
        </fieldset>
        <Button type="submit" disabled={!canSubmit(mode, input)}>
          Search
        </Button>
        <Button
          type="button"
          variant="ghost"
          aria-expanded={showFilters}
          onClick={() => {
            setShowFilters((s) => !s);
          }}
        >
          <SlidersHorizontalIcon aria-hidden="true" /> Filters
        </Button>
      </form>
      {showFilters ? (
        <div className="border-border rounded-lg border p-4">
          <DocumentFiltersPanel
            idPrefix="s"
            filters={filters}
            onChange={(next) => {
              writeUrl({ filters: next });
            }}
          />
        </div>
      ) : null}

      {tooShort ? (
        <p className="text-muted-foreground text-sm">Type at least {SEMANTIC_MIN_CHARS} characters.</p>
      ) : null}

      {data?.degraded ? (
        <div
          role="status"
          className="border-warning/40 bg-warning/10 flex items-start gap-2 rounded-md border p-3 text-sm"
        >
          <AlertTriangleIcon className="text-warning mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            Showing keyword results only: meaning-based search is unavailable right now
            {data.degraded_reason ? ` (${data.degraded_reason})` : ""}.
          </span>
        </div>
      ) : null}

      <p className="sr-only" aria-live="polite">
        {isFetching ? "Searching" : data ? `${data.total} results` : ""}
      </p>

      {!committed ? (
        <EmptyState title="Search by invoice number, supplier, customer or what the invoice is about" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} title="Search failed" />
      ) : !data ? (
        <div className="space-y-3" aria-busy="true">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : data.items.length === 0 ? (
        <EmptyState title={`No results for “${committed}”`}>
          <p className="text-muted-foreground text-sm">
            Try fewer words, another mode, or clear the filters.
          </p>
        </EmptyState>
      ) : (
        <>
          <p className="text-muted-foreground text-sm">
            {data.total} result{data.total === 1 ? "" : "s"}
          </p>
          <ol className={cn("space-y-3", isFetching && "opacity-60")}>
            {data.items.map((hit) => (
              <Result key={`${hit.document_id}-${hit.extraction_id ?? "doc"}`} hit={hit} />
            ))}
          </ol>
          <nav aria-label="Result pages" className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset === 0}
              onClick={() => {
                writeUrl({ offset: Math.max(0, offset - SEARCH_PAGE_SIZE) });
              }}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={offset + SEARCH_PAGE_SIZE >= data.total}
              onClick={() => {
                writeUrl({ offset: offset + SEARCH_PAGE_SIZE });
              }}
            >
              Next
            </Button>
          </nav>
        </>
      )}
    </div>
  );
}
