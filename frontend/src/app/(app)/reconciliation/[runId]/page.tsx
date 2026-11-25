import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { RunReport } from "@/features/reconciliation/components/run-report";

export const metadata: Metadata = { title: "Reconciliation report" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  if (!UUID.test(runId)) {
    notFound();
  }
  return (
    <div className="space-y-4">
      <Link href="/reconciliation" className="text-primary text-sm hover:underline">
        ← New check
      </Link>
      <h1 className="text-2xl font-semibold">Reconciliation report</h1>
      <RunReport runId={runId} />
    </div>
  );
}
