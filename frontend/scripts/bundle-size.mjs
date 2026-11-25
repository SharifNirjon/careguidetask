// Client JavaScript size after `npm run build` (raw and gzip), largest chunks first.
// Next 16 no longer prints per-route sizes, so this reports what the browser downloads.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const ROOT = ".next/static/chunks";
const KB = 1024;
const TOP = 8;

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      yield* walk(path);
    } else if (name.endsWith(".js")) {
      yield path;
    }
  }
}

const chunks = [...walk(ROOT)].map((path) => {
  const bytes = readFileSync(path);
  return { path, raw: bytes.length, gzip: gzipSync(bytes).length };
});
const total = chunks.reduce((acc, c) => ({ raw: acc.raw + c.raw, gzip: acc.gzip + c.gzip }), { raw: 0, gzip: 0 });
console.log(`client JS: ${chunks.length} chunks, ${(total.raw / KB).toFixed(0)} KB raw, ${(total.gzip / KB).toFixed(0)} KB gzip`);
for (const c of chunks.sort((a, b) => b.gzip - a.gzip).slice(0, TOP)) {
  console.log(`  ${(c.gzip / KB).toFixed(1).padStart(6)} KB gzip  ${c.path}`);
}
