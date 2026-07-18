/**
 * Ops dashboard auth helpers.
 *
 * Temporary shared-secret gate for the ``/ops`` operator dashboard,
 * intended to last until a real identity model lands. The model:
 *
 *   1. Operator visits `/ops/login`, types the shared secret, POSTs to
 *      `/ops/login` (Route Handler).
 *   2. The handler compares to `ADMIN_SECRET` (server-side env var),
 *      and on match sets a short-lived, HMAC-signed HttpOnly session cookie.
 *   3. Every `/ops/*` server component calls `requireOpsSession()` which
 *      reads the cookie and re-verifies it matches `ADMIN_SECRET`.
 *   4. After verifying the signed cookie, the server reads the real secret
 *      from its environment and passes that to the bridge as `X-Admin-Token`.
 *      The browser never stores the operator credential itself.
 *
 * **Not a replacement for real auth.** This exists so the first operator
 * can see cross-tenant metrics before we build IAM-backed identity.
 * Revisit once we have it.
 */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { getAdminSecret } from "./env";

/** Cookie name for the ops shared-secret session. */
export const OPS_COOKIE_NAME = "ops_session";

const OPS_SESSION_TTL_SECONDS = 3600;
const OPS_SESSION_DOMAIN = "agent-ops-session-v1";

/** Cookie attributes. One-hour TTL; matches the tenant session. */
export const OPS_COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/ops",
  maxAge: OPS_SESSION_TTL_SECONDS,
};

/** Mint an opaque-looking session token signed by the operator secret. */
export function makeOpsSession(adminSecret: string): string {
  const nonce = randomBytes(24).toString("hex");
  const timestamp = Math.floor(Date.now() / 1000);
  const payload = `${OPS_SESSION_DOMAIN}.${nonce}.${timestamp}`;
  const signature = createHmac("sha256", adminSecret)
    .update(payload)
    .digest("hex");
  return `${nonce}.${timestamp}.${signature}`;
}

/** Verify an ops session without ever placing the raw admin secret in a cookie. */
export function verifyOpsSession(
  token: string | undefined,
  adminSecret: string,
): boolean {
  if (!token) return false;
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  const [nonce, timestampRaw, signature] = parts;
  if (!/^[a-f0-9]{48}$/.test(nonce) || !/^[a-f0-9]{64}$/.test(signature)) {
    return false;
  }
  if (!/^\d+$/.test(timestampRaw)) return false;
  const timestamp = Number.parseInt(timestampRaw, 10);
  if (!Number.isSafeInteger(timestamp)) return false;
  const now = Math.floor(Date.now() / 1000);
  if (timestamp > now + 30 || now - timestamp > OPS_SESSION_TTL_SECONDS) {
    return false;
  }
  const payload = `${OPS_SESSION_DOMAIN}.${nonce}.${timestamp}`;
  const expected = createHmac("sha256", adminSecret)
    .update(payload)
    .digest("hex");
  const actualBuffer = Buffer.from(signature, "hex");
  const expectedBuffer = Buffer.from(expected, "hex");
  return (
    actualBuffer.length === expectedBuffer.length &&
    timingSafeEqual(actualBuffer, expectedBuffer)
  );
}

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
  if (!verifyOpsSession(cookie?.value, expected)) {
    // Missing, expired, forged, or invalidated by a secret rotation.
    redirect("/ops/login");
  }
  // Only server-side code receives the real credential. The signed cookie is
  // a session proof and cannot be replayed directly against the bridge.
  return expected;
}
