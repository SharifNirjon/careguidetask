/**
 * Session endpoints of THIS app (not the backend API): /api/auth/*.
 * The session cookie is httpOnly, so the browser code never sees it.
 */
export async function login(
  username: string,
  password: string,
): Promise<{ ok: true } | { ok: false; message: string }> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  }).catch(() => null);
  if (!response) {
    return { ok: false, message: "Network error. Check your connection and try again." };
  }
  if (response.ok) {
    return { ok: true };
  }
  const body: unknown = await response.json().catch(() => null);
  const detail =
    typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string"
      ? body.detail
      : "Sign-in failed.";
  return { ok: false, message: detail };
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => undefined);
}

/** Only same-site relative paths: never redirect to another origin after login. */
export function safeNextPath(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) {
    return "/documents";
  }
  return next;
}
