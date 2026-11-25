import type { Metadata } from "next";
import { Suspense } from "react";

import { DocumentsTableSkeleton } from "@/features/documents/components/documents-table";
import { DocumentsView } from "@/features/documents/components/documents-view";

export const metadata: Metadata = { title: "Documents" };

export default function DocumentsPage() {
  return (
    <Suspense fallback={<DocumentsTableSkeleton />}>
      <DocumentsView />
    </Suspense>
  );
}
