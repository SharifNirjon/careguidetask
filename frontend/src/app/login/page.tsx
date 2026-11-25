import type { Metadata } from "next";
import { Suspense } from "react";

import { LoginForm } from "@/features/auth/components/login-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <main id="main" className="bg-muted flex min-h-screen items-center justify-center p-4">
      <div className="border-border bg-background w-full max-w-sm space-y-6 rounded-lg border p-8 shadow-sm">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">Invoice review</h1>
          <p className="text-muted-foreground text-sm">Sign in to review extracted invoices.</p>
        </div>
        <Suspense>
          <LoginForm />
        </Suspense>
      </div>
    </main>
  );
}
