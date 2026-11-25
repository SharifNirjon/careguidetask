"use client";

import { ChevronLeftIcon, ChevronRightIcon, DownloadIcon, ZoomInIcon, ZoomOutIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { proxyUrl } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { SheetView } from "./sheet-view";

const PREVIEW_DPI = 110;
const THUMB_DPI = 50;
const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;
const DEFAULT_ZOOM = 2; // index into ZOOM_STEPS (100 %)
const VISUAL_TYPES = new Set(["pdf", "png", "jpeg", "tiff"]);

export interface ViewerProps {
  documentId: string;
  fileType: string;
  filename: string;
  unitCount: number;
  page: number;
  onPageChange: (page: number) => void;
  /** Spreadsheet evidence to highlight ("sheet X row N col H"). */
  evidence: string | null;
}

function imageUrl(documentId: string, unit: number, dpi: number): string {
  return proxyUrl(`/api/v1/documents/${documentId}/units/${unit}/image?dpi=${dpi}`);
}

export function DocumentViewer({
  documentId,
  fileType,
  filename,
  unitCount,
  page,
  onPageChange,
  evidence,
}: ViewerProps) {
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);
  const scale = ZOOM_STEPS[zoom] ?? 1;
  const pages = Math.max(unitCount, 1);
  const visual = VISUAL_TYPES.has(fileType);
  const thumbRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    thumbRefs.current[page]?.scrollIntoView({ block: "nearest" });
  }, [page]);

  const go = (next: number) => {
    onPageChange(Math.min(Math.max(next, 0), pages - 1));
  };

  return (
    <section
      aria-label="Document viewer"
      tabIndex={-1}
      className="border-border focus-visible:outline-ring flex h-full min-h-0 flex-col rounded-lg border focus-visible:outline-2"
      onKeyDown={(event) => {
        if (event.key === "]" || event.key === "PageDown") {
          event.preventDefault();
          go(page + 1);
        } else if (event.key === "[" || event.key === "PageUp") {
          event.preventDefault();
          go(page - 1);
        }
      }}
    >
      <div className="border-border flex flex-wrap items-center gap-2 border-b p-2">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Previous page"
          disabled={page <= 0}
          onClick={() => {
            go(page - 1);
          }}
        >
          <ChevronLeftIcon />
        </Button>
        <span className="text-sm tabular-nums" aria-live="polite">
          {visual ? "Page" : "Sheet"} {page + 1} of {pages}
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Next page"
          disabled={page >= pages - 1}
          onClick={() => {
            go(page + 1);
          }}
        >
          <ChevronRightIcon />
        </Button>
        {visual ? (
          <>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Zoom out"
              disabled={zoom === 0}
              onClick={() => {
                setZoom((z) => z - 1);
              }}
            >
              <ZoomOutIcon />
            </Button>
            <span className="w-12 text-center text-sm tabular-nums">{Math.round(scale * 100)}%</span>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Zoom in"
              disabled={zoom === ZOOM_STEPS.length - 1}
              onClick={() => {
                setZoom((z) => z + 1);
              }}
            >
              <ZoomInIcon />
            </Button>
          </>
        ) : null}
        <Button variant="outline" size="sm" className="ml-auto" asChild>
          <a href={proxyUrl(`/api/v1/documents/${documentId}/file`)} download={filename}>
            <DownloadIcon aria-hidden="true" /> Original
          </a>
        </Button>
      </div>

      <div className="flex min-h-0 flex-1">
        {visual && pages > 1 ? (
          <ol
            aria-label="Pages"
            className="border-border w-24 shrink-0 space-y-2 overflow-y-auto border-r p-2"
          >
            {Array.from({ length: pages }, (_, i) => (
              <li key={i}>
                <button
                  ref={(el) => {
                    thumbRefs.current[i] = el;
                  }}
                  type="button"
                  aria-label={`Page ${i + 1}`}
                  aria-current={i === page ? "page" : undefined}
                  onClick={() => {
                    go(i);
                  }}
                  className={cn(
                    "focus-visible:outline-ring block w-full rounded border-2 focus-visible:outline-2",
                    i === page ? "border-primary" : "border-transparent",
                  )}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element -- authenticated proxy images, not static assets */}
                  <img src={imageUrl(documentId, i, THUMB_DPI)} alt="" loading="lazy" className="w-full" />
                  <span className="text-xs">{i + 1}</span>
                </button>
              </li>
            ))}
          </ol>
        ) : null}
        <div
          role="region"
          aria-label={`Page ${page + 1} preview`}
          tabIndex={0}
          className="bg-muted focus-visible:ring-ring min-h-0 flex-1 overflow-auto p-4 focus-visible:ring-2 focus-visible:outline-none"
        >
          {visual ? (
            // eslint-disable-next-line @next/next/no-img-element -- authenticated proxy images, not static assets
            <img
              key={page}
              src={imageUrl(documentId, page, PREVIEW_DPI)}
              alt={`Page ${page + 1} of ${filename}`}
              style={{ width: `${scale * 100}%` }}
              className="bg-background mx-auto max-w-none shadow"
            />
          ) : (
            <SheetView documentId={documentId} unitIndex={page} evidence={evidence} />
          )}
        </div>
      </div>
    </section>
  );
}
