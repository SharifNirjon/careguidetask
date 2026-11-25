import type { Metadata } from "next";

import { ReconciliationView } from "@/features/reconciliation/components/reconciliation-view";

export const metadata: Metadata = { title: "Reconciliation" };

export default function ReconciliationPage() {
  return <ReconciliationView />;
}
