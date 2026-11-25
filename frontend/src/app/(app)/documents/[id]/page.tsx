import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/misc";
import { ReviewScreen } from "@/features/review/components/review-screen";

export const metadata: Metadata = { title: "Review" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) {
    notFound();
  }
  return (
    <Suspense fallback={<Skeleton className="h-[70vh] w-full" />}>
      <ReviewScreen documentId={id} />
    </Suspense>
  );
}
