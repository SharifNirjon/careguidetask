"use client";

import { CopyIcon, XCircleIcon } from "lucide-react";
import Link from "next/link";

import { DocumentStatusBadge } from "@/components/status-badges";
import { useDocument } from "@/features/documents/hooks";
import { messageFor } from "@/lib/api/errors";
import type { UploadOutcome } from "@/lib/api/types";

/** Live processing status of one queued file (ETag polling with backoff, see useDocument). */
function Processing({ id }: { id: string }) {
  const { data } = useDocument(id);
  if (!data) {
    return <span className="text-muted-foreground text-sm">Queued</span>;
  }
  return (
    <span className="flex items-center gap-2">
      <DocumentStatusBadge status={data.status} />
      {data.progress.stage ? (
        <span className="text-muted-foreground text-xs">
          {data.progress.stage} · {data.progress.percent}%
        </span>
      ) : null}
    </span>
  );
}

export function UploadOutcomes({ outcomes }: { outcomes: UploadOutcome[] }) {
  const queued = outcomes.filter((o) => o.status === "queued").length;
  return (
    <div className="space-y-3">
      <p aria-live="polite" className="text-sm">
        {queued} of {outcomes.length} files accepted.
      </p>
      <ul className="max-h-80 space-y-2 overflow-y-auto">
        {outcomes.map((outcome, index) => (
          <li key={`${outcome.filename}-${index}`} className="border-border rounded border px-3 py-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              {outcome.document_id ? (
                <Link
                  href={`/documents/${outcome.document_id}`}
                  className="text-primary truncate font-medium hover:underline"
                >
                  {outcome.filename}
                </Link>
              ) : (
                <span className="truncate font-medium">{outcome.filename}</span>
              )}
              {outcome.document_id ? (
                <Processing id={outcome.document_id} />
              ) : (
                <span className="text-destructive inline-flex items-center gap-1">
                  <XCircleIcon className="size-4" aria-hidden="true" /> Rejected
                </span>
              )}
            </div>
            {outcome.duplicate_of_document_id ? (
              <p className="text-muted-foreground mt-1 flex items-center gap-1 text-xs">
                <CopyIcon className="size-3" aria-hidden="true" /> Duplicate of{" "}
                <Link
                  href={`/documents/${outcome.duplicate_of_document_id}`}
                  className="text-primary hover:underline"
                >
                  an earlier upload
                </Link>
              </p>
            ) : null}
            {outcome.error ? (
              <p className="text-destructive mt-1 text-xs">
                {messageFor(outcome.error.code, outcome.error.detail)}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
