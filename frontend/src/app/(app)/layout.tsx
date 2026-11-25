import Link from "next/link";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { LogoutButton } from "@/features/auth/components/logout-button";
import { getSession, isLoggedIn } from "@/lib/server/session";

import { NavLinks } from "./nav-links";

/** Signed-in shell (Server Component). The proxy already guards; this re-checks. */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const session = await getSession();
  if (!isLoggedIn(session)) {
    redirect("/login");
  }
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-border bg-background border-b">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-6 px-4">
          <Link href="/documents" className="font-semibold">
            Invoice review
          </Link>
          <NavLinks />
          <div className="text-muted-foreground ml-auto flex items-center gap-3 text-sm">
            <span>{session.username}</span>
            <LogoutButton />
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-6">
        {children}
      </main>
    </div>
  );
}
