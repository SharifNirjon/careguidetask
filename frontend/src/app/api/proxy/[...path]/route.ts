import { getSession, isLoggedIn } from "@/lib/server/session";
import { serverEnv } from "@/lib/server/env";
import { handleProxy } from "@/lib/server/proxy-handler";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface Context {
  params: Promise<{ path: string[] }>;
}

async function proxy(request: Request, context: Context): Promise<Response> {
  const env = serverEnv();
  const { path } = await context.params;
  return handleProxy(request, path, {
    backendUrl: env.BACKEND_URL,
    apiKey: env.API_KEY,
    expectedOrigin: env.APP_ORIGIN ?? new URL(request.url).origin,
    isAuthenticated: async () => isLoggedIn(await getSession()),
  });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
