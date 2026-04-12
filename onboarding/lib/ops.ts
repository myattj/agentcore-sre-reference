/**
 * Ops dashboard auth helpers.
 *
 * Temporary shared-secret gate for the ``/ops`` operator dashboard,
 * intended to last until a real identity model lands. The model:
 *
 *   1. Operator visits `/ops/login`, types the shared secret, POSTs to
 *      `/ops/login` (Route Handler).
 *   2. The handler compares to `ADMIN_SECRET` (server-side env var),
 *      and on match sets an HttpOnly cookie whose value IS the secret.
 *   3. Every `/ops/*` server component calls `requireOpsSession()` which
 *      reads the cookie and re-verifies it matches `ADMIN_SECRET`.
 *   4. The cookie value is what we pass to the bridge as `X-Admin-Token`
 *      (see `lib/bridge.ts:adminFetch`).
 *
 * Why store the secret in the cookie instead of a derived session id?
 * Because we don't have a session table and the bridge expects the raw
 * secret in its header. Cookie is HttpOnly + Secure in prod so JS can't
 * read it; the blast radius of a cookie leak is identical to an env-var
 * leak (they're the same value).
 *
 * **Not a replacement for real auth.** This exists so the first operator
 * can see cross-tenant metrics before we build IAM-backed identity.
 * Revisit once we have it.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { getAdminSecret } from "./env";

/** Cookie name for the ops shared-secret session. */
export const OPS_COOKIE_NAME = "ops_admin_token";

/** Cookie attributes. One-hour TTL; matches the tenant session. */
export const OPS_COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/ops",
  maxAge: 3600,
};

/**
 * Read the ops cookie and verify it matches the configured admin secret.
 *
 * Three failure modes:
 *   - ``ADMIN_SECRET`` unset → throws an Error with a clear message
 *     (the /ops routes catch this and render "ops disabled").
 *   - No cookie → redirects to ``/ops/login``.
 *   - Cookie value mismatch (rotated secret) → redirects to ``/ops/login``.
 *
 * Returns the raw admin secret string on success so callers can pass
 * it to ``getOpsRoster`` / ``getOpsTenantMetrics``.
 */
export async function requireOpsSession(): Promise<string> {
  const expected = getAdminSecret();
  if (!expected) {
    // Signal via throw rather than redirect — the page-level catch can
    // render a helpful "ops is disabled on this deploy" screen instead
    // of bouncing to a login that would just re-throw.
    throw new Error("ops-disabled");
  }
  const store = await cookies();
  const cookie = store.get(OPS_COOKIE_NAME);
  if (!cookie?.value) {
    redirect("/ops/login");
  }
  if (cookie.value !== expected) {
    // Secret has been rotated — force a fresh login.
    redirect("/ops/login");
  }
  return cookie.value;
}
