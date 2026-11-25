"use client";

import { EyeIcon, PlayIcon, SlidersHorizontalIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/error-state";
import { Label, NativeSelect } from "@/components/ui/form-controls";
import { UploadDialog } from "@/features/upload/components/upload-dialog";

import { useDocuments, useListParams, useOpenNextToReview } from "../hooks";
import { hasActiveFilters, NO_FILTERS, SORT_FIELDS, type ListParams } from "../schemas";
import { ClearFiltersButton, DocumentFiltersPanel } from "./document-filters";
import { DocumentsTable, DocumentsTableSkeleton } from "./documents-table";

const SORT_LABELS: Record<ListParams["sort"], string> = {
  created_at: "Uploaded",
  invoice_date: "Invoice date",
  total: "Total",
};

export function DocumentsView() {
  const [params, setParams] = useListParams();
  const [showFilters, setShowFilters] = useState(hasActiveFilters(params));
  const { data, error, isPending, isFetching, refetch } = useDocuments(params);
  const openNext = useOpenNextToReview();
  const needsReviewOnly = params.status === "needs_review";

  const startReviewing = () => {
    openNext.mutate(undefined, {
      onSuccess: (id) => {
        if (!id) {
          toast.success("Nothing needs review right now.");
        }
      },
      onError: () => toast.error("Couldn't find the next document to review."),
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-2xl font-semibold">Documents</h1>
        <Button variant="outline" onClick={startReviewing} disabled={openNext.isPending}>
          <PlayIcon aria-hidden="true" /> Start reviewing
        </Button>
        <UploadDialog />
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <Button
          variant={needsReviewOnly ? "default" : "outline"}
          size="sm"
          aria-pressed={needsReviewOnly}
          onClick={() => {
            setParams({ status: needsReviewOnly ? undefined : "needs_review" });
          }}
        >
          <EyeIcon aria-hidden="true" /> Needs review
        </Button>
        <Button
          variant="ghost"
          size="sm"
          aria-expanded={showFilters}
          aria-controls="filters"
          onClick={() => {
            setShowFilters((s) => !s);
          }}
        >
          <SlidersHorizontalIcon aria-hidden="true" /> Filters
        </Button>
        {hasActiveFilters(params) ? (
          <ClearFiltersButton
            onClear={() => {
              setParams(NO_FILTERS);
            }}
          />
        ) : null}
        <div className="ml-auto flex items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor="sort">Sort by</Label>
            <NativeSelect
              id="sort"
              value={params.sort}
              onChange={(e) => {
                setParams({ sort: SORT_FIELDS.find((s) => s === e.target.value) ?? "created_at" });
              }}
            >
              {SORT_FIELDS.map((s) => (
                <option key={s} value={s}>
                  {SORT_LABELS[s]}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="space-y-1">
            <Label htmlFor="order">Order</Label>
            <NativeSelect
              id="order"
              value={params.order}
              onChange={(e) => {
                setParams({ order: e.target.value === "asc" ? "asc" : "desc" });
              }}
            >
              <option value="desc">Descending</option>
              <option value="asc">Ascending</option>
            </NativeSelect>
          </div>
        </div>
      </div>

      {showFilters ? (
        <div id="filters" className="border-border rounded-lg border p-4">
          <DocumentFiltersPanel filters={params} onChange={setParams} />
        </div>
      ) : null}

      <p className="sr-only" aria-live="polite">
        {isFetching ? "Updating documents" : data ? `${data.items.length} documents shown` : ""}
      </p>

      {isPending ? (
        <DocumentsTableSkeleton />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} title="Couldn't load documents" />
      ) : data.items.length === 0 ? (
        <EmptyState
          title={hasActiveFilters(params) ? "No documents match these filters" : "No documents yet"}
        >
          {hasActiveFilters(params) ? (
            <ClearFiltersButton
              onClear={() => {
                setParams(NO_FILTERS);
              }}
            />
          ) : (
            <UploadDialog />
          )}
        </EmptyState>
      ) : (
        <>
          <DocumentsTable documents={data.items} caption="Documents" />
          <nav aria-label="Pagination" className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!params.cursor}
              onClick={() => {
                setParams({ cursor: undefined });
              }}
            >
              First page
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!data.next_cursor}
              onClick={() => {
                setParams({ cursor: data.next_cursor ?? undefined });
              }}
            >
              Next page
            </Button>
          </nav>
        </>
      )}
    </div>
  );
}
