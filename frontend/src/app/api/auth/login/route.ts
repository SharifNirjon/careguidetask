import { z } from "zod";

import { safeNextPath } from "@/features/auth/api";
import { verifyCredentials } from "@/lib/server/credentials";
import { serverEnv } from "@/lib/server/env";
import { allowLoginAttempt, resetLoginAttempts } from "@/lib/server/login-throttle";
import { isSameOriginRequest } from "@/lib/server/proxy-rules";
import { getSession } from "@/lib/server/session";

export const runtime = "nodejs";

const body = z.object({
  username: z.string().min(1).max(200),
  password: z.string().min(1).max(500),
});

function fail(status: number, detail: string): Response {
  return Response.json({ detail }, { status });
}

/**
 * 303 to a same-origin path (the no-JavaScript form fallback). The Location is
 * relative: behind Docker, request.url carries the bind address (0.0.0.0).
 */
function redirectTo(path: string): Response {
  return new Response(null, { status: 303, headers: { location: path } });
}

/** JSON from the app's own fetch, or a native form post when JavaScript failed to load. */
async function readBody(request: Request, isForm: boolean): Promise<{ raw: unknown; next: string | null }> {
  if (!isForm) {
    return { raw: await request.json().catch(() => null), next: null };
  }
  const form = await request.formData().catch(() => null);
  const next = form?.get("next");
  return {
    raw: form ? { username: form.get("username"), password: form.get("password") } : null,
    next: typeof next === "string" ? next : null,
  };
}

export async function POST(request: Request): Promise<Response> {
  const env = serverEnv();
  if (!isSameOriginRequest(request.headers, env.APP_ORIGIN ?? new URL(request.url).origin)) {
    return fail(403, "Cross-origin requests are not allowed.");
  }
  const isForm = (request.headers.get("content-type") ?? "").startsWith("application/x-www-form-urlencoded");
  const client = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "local";
  if (!allowLoginAttempt(client)) {
    return isForm
      ? redirectTo("/login?error=throttled")
      : fail(429, "Too many attempts. Wait a minute and try again.");
  }
  const { raw, next } = await readBody(request, isForm);
  const parsed = body.safeParse(raw);
  if (!parsed.success || !(await verifyCredentials(parsed.data.username, parsed.data.password))) {
    return isForm ? redirectTo("/login?error=invalid") : fail(401, "Incorrect username or password.");
  }
  resetLoginAttempts(client);
  const session = await getSession();
  session.username = parsed.data.username;
  session.loggedInAt = Date.now();
  await session.save();
  if (isForm) {
    return redirectTo(safeNextPath(next));
  }
  return Response.json({ username: session.username });
}
