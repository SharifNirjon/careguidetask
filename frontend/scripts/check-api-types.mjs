// Fails when src/lib/api/schema.d.ts is stale relative to ../docs/openapi.json.
// Regenerates into a temp file and compares: run `npm run gen:api` to fix.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "api-types-"));
const fresh = join(dir, "schema.d.ts");
try {
  execFileSync(
    process.execPath,
    ["node_modules/openapi-typescript/bin/cli.js", "../docs/openapi.json", "-o", fresh],
    {
      stdio: "ignore",
    },
  );
  const normalize = (text) => text.replace(/\r\n/g, "\n");
  const current = normalize(readFileSync("src/lib/api/schema.d.ts", "utf8"));
  if (normalize(readFileSync(fresh, "utf8")) !== current) {
    console.error("src/lib/api/schema.d.ts is stale: run `npm run gen:api` and commit the result.");
    process.exit(1);
  }
  console.log("API types are up to date with docs/openapi.json.");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
