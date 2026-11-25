// Fails if the API key (or any server-only secret) appears in the CLIENT bundle.
// Run after `npm run build` with the same environment the build used:
//   API_KEY=... SESSION_SECRET=... npm run check:bundle
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const CLIENT_DIRS = [".next/static", ".next/standalone/.next/static"];
const SECRETS = ["API_KEY", "SESSION_SECRET", "DEMO_PASSWORD_HASH"];
const MIN_SECRET_CHARS = 8;

function* files(dir) {
  let entries;
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      yield* files(path);
    } else {
      yield path;
    }
  }
}

const values = SECRETS.map((name) => [name, process.env[name] ?? ""]).filter(
  ([, v]) => v.length >= MIN_SECRET_CHARS,
);
if (values.length === 0) {
  console.error(`Set ${SECRETS.join(", ")} (as used for the build) so the check knows what to look for.`);
  process.exit(2);
}

let scanned = 0;
const leaks = [];
for (const dir of CLIENT_DIRS) {
  for (const file of files(dir)) {
    scanned += 1;
    const text = readFileSync(file, "latin1");
    for (const [name, value] of values) {
      if (text.includes(value)) {
        leaks.push(`${name} found in ${file}`);
      }
    }
  }
}
if (scanned === 0) {
  console.error("No client bundle found: run `npm run build` first.");
  process.exit(2);
}
if (leaks.length > 0) {
  console.error(`SECRET LEAK in the client bundle:\n${leaks.join("\n")}`);
  process.exit(1);
}
console.log(`OK: ${scanned} client files scanned; no server secret found.`);
