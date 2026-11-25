import { unsealData } from "iron-session";
import { NextResponse, type NextRequest } from "next/server";

import { buildCsp } from "@/lib/csp";

/**
 * Next.js 16 proxy (formerly middleware), runs before every matched request:
 * 1. Auth guard: everything except /login and the login endpoint needs a session.
 *    Route handlers re-check the session themselves (defense in depth).
 * 2. CSP with a per-request nonce. Next reads the nonce from the request's
 *    Content-Security-Policy header and applies it to its own scripts, so no
 *    'unsafe-inline' is needed for scripts.
 */
const PUBLIC_PATHS = new Set(["/login", "/api/auth/login"]);
const SESSION_COOKIE = "cg_session";

async function hasSession(request: NextRequest): Promise<boolean> {
  const sealed = request.cookies.get(SESSION_COOKIE)?.value;
  const password = process.env.SESSION_SECRET;
  if (!sealed || !password) {
    return false;
  }
  try {
    const data = await unsealData<{ username?: string }>(sealed, { password });
    return typeof data.username === "string" && data.username.length > 0;
  } catch {
    return false;
  }
}

export async function proxy(request: NextRequest): Promise<NextResponse> {
  const { pathname, search } = request.nextUrl;
  if (!PUBLIC_PATHS.has(pathname) && !(await hasSession(request))) {
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ code: "unauthenticated", detail: "Please sign in." }, { status: 401 });
    }
    const login = new URL("/login", request.url);
    if (pathname !== "/") {
      login.searchParams.set("next", `${pathname}${search}`);
    }
    return NextResponse.redirect(login);
  }

  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = buildCsp(nonce, process.env.NODE_ENV !== "production");
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
