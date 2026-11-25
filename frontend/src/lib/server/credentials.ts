import "server-only";

import { randomUUID } from "node:crypto";

import bcrypt from "bcryptjs";

import { serverEnv } from "./env";

const BCRYPT_COST = 12;
let dummyHash: string | undefined;

/** A real hash of a random value: an unknown username costs as much as a known one. */
function dummy(): string {
  dummyHash ??= bcrypt.hashSync(randomUUID(), BCRYPT_COST);
  return dummyHash;
}

/**
 * bcrypt always runs, whether or not the username matches, so response timing
 * doesn't reveal valid usernames.
 */
export async function verifyCredentials(username: string, password: string): Promise<boolean> {
  const env = serverEnv();
  const known = username === env.DEMO_USERNAME;
  const ok = await bcrypt.compare(password, known ? env.DEMO_PASSWORD_HASH : dummy());
  return known && ok;
}
