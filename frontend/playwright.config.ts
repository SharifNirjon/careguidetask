import { readFileSync } from "node:fs";

import { defineConfig, devices } from "@playwright/test";

/**
 * E2E against the running docker compose stack (UI on :3000). Processing calls
 * the real LLM, so the timeout is generous. Credentials come from E2E_* in the
 * repo's .env (local only, gitignored) unless already set in the environment.
 */
function loadE2eEnv(): void {
  let text = "";
  try {
    text = readFileSync("../.env", "utf8");
  } catch {
    return;
  }
  for (const line of text.split(/\r?\n/)) {
    const match = /^(E2E_[A-Z_]+)=(.*)$/.exec(line);
    if (match?.[1] && match[2] !== undefined && process.env[match[1]] === undefined) {
      process.env[match[1]] = match[2];
    }
  }
}
loadE2eEnv();

const MINUTE = 60_000;

export default defineConfig({
  testDir: "./e2e",
  timeout: 20 * MINUTE,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
});
