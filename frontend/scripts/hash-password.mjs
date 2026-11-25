// Prints a bcrypt hash for DEMO_PASSWORD_HASH. The password is read from stdin
// (not an argument), so it never lands in shell history or the process list.
//
//   npm run hash-password            (type the password, then Enter)
//   echo -n "secret" | npm run -s hash-password
import { createInterface } from "node:readline";

import bcrypt from "bcryptjs";

const COST = 12;

const rl = createInterface({ input: process.stdin, output: process.stderr, terminal: process.stdin.isTTY });
if (process.stdin.isTTY) {
  process.stderr.write("Password: ");
}
rl.once("line", (password) => {
  rl.close();
  if (password.length < 8) {
    console.error("Use at least 8 characters.");
    process.exit(1);
  }
  // In a .env file, a bcrypt hash's "$" must be escaped for Docker Compose: write "$$".
  const hash = bcrypt.hashSync(password, COST);
  console.log(hash);
  console.error(`\nFor docker-compose .env files, escape the dollars: ${hash.replaceAll("$", "$$$$")}`);
});
