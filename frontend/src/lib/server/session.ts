import "server-only";

import { getIronSession, type SessionOptions } from "iron-session";
import { cookies } from "next/headers";

import { serverEnv } from "./env";

export const SESSION_COOKIE = "cg_session";
const SESSION_TTL_S = 8 * 60 * 60; // one working day

export interface SessionData {
  username?: string;
  loggedInAt?: number;
}

export function sessionOptions(): SessionOptions {
  const env = serverEnv();
  return {
    cookieName: SESSION_COOKIE,
    password: env.SESSION_SECRET,
    ttl: SESSION_TTL_S,
    cookieOptions: {
      httpOnly: true,
      secure: env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
    },
  };
}

export async function getSession() {
  return getIronSession<SessionData>(await cookies(), sessionOptions());
}

export function isLoggedIn(session: SessionData): boolean {
  return typeof session.username === "string" && session.username.length > 0;
}
