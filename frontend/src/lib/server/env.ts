import "server-only";

import { z } from "zod";

/**
 * Server-only configuration. Read lazily (not at import) so `next build` works
 * without secrets, and validated once so a misconfiguration fails loudly at the
 * first request instead of producing confusing 500s later.
 *
 * Nothing here may ever be prefixed NEXT_PUBLIC_: these values must not reach
 * the browser bundle (scripts/check-bundle.mjs verifies the API key doesn't).
 */
const schema = z.object({
  BACKEND_URL: z.url(),
  API_KEY: z.string().min(16),
  SESSION_SECRET: z.string().min(32, "SESSION_SECRET must be at least 32 characters"),
  DEMO_USERNAME: z.string().min(1),
  DEMO_PASSWORD_HASH: z.string().startsWith("$2", "DEMO_PASSWORD_HASH must be a bcrypt hash"),
  // Public origin of this app (behind a reverse proxy the Host header may differ).
  APP_ORIGIN: z.url().optional(),
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});

export type ServerEnv = z.infer<typeof schema>;

let cached: ServerEnv | undefined;

export function serverEnv(): ServerEnv {
  cached ??= schema.parse(process.env);
  return cached;
}
