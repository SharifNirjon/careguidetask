import { serverEnv } from "@/lib/server/env";
import { isSameOriginRequest } from "@/lib/server/proxy-rules";
import { getSession } from "@/lib/server/session";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const env = serverEnv();
  if (!isSameOriginRequest(request.headers, env.APP_ORIGIN ?? new URL(request.url).origin)) {
    return Response.json({ detail: "Cross-origin requests are not allowed." }, { status: 403 });
  }
  const session = await getSession();
  session.destroy();
  return new Response(null, { status: 204 });
}
