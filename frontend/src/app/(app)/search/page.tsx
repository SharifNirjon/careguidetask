import type { Metadata } from "next";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/misc";
import { SearchView } from "@/features/search/components/search-view";

export const metadata: Metadata = { title: "Search" };

export default function SearchPage() {
  return (
    <Suspense fallback={<Skeleton className="h-40 w-full" />}>
      <SearchView />
    </Suspense>
  );
}
