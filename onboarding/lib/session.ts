/**
 * Session token verification + cookie helpers.
 *
 * The bridge mints session tokens in `bridge/bridge/slack_oauth.py`
 * (`make_session_token`) at the end of the OAuth callback. Format:
 *
 *     {tenant_id}.{nonce}.{ts}.{hmac_hex}
 *
 * where the HMAC is SHA-256 over `{tenant_id}.{nonce}.{ts}` using
 * `BRIDGE_OAUTH_STATE_SECRET` as the key. The token has a 60-minute TTL.
 *
 * The Next.js side never MINTS tokens — only the bridge does. We only
 * verify them and forward them as `Authorization: Bearer <token>` to
 * the bridge API. Cross-tenant isolation is enforced by the bridge's
 * `require_session_token` dependency, which asserts that the token's
 * embedded tenant matches the URL path.
 *
 * KEEP IN SYNC with `bridge/bridge/slack_oauth.py:_sign_session` and
 * `verify_session_token`. If you change the format, change both sides.
 */
import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { getStateSecret } from "./env";

/** Must match `bridge/bridge/slack_oauth.py:_SESSION_TTL_SECONDS`. */
export const SESSION_TTL_SECONDS = 3600;

/** Cookie name for the onboarding session. */
export const SESSION_COOKIE_NAME = "tenant_session";

/** Cookie attributes — see CLAUDE.md gotcha #25. */
export const SESSION_COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/",
  maxAge: SESSION_TTL_SECONDS,
};

/**
 * Verify a session token. Returns the embedded `tenant_id` on success,
 * or `null` if the token is missing/malformed/expired/tampered.
 *
 * Constant-time comparison via `timingSafeEqual` mirrors Python's
 * `hmac.compare_digest` on the bridge side.
 */
export function verifySessionToken(token: string | undefined | null): string | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 4) return null;
  const [tenantId, nonce, tsStr, sig] = parts;
  if (!tenantId || !nonce || !sig) return null;

  const ts = Number.parseInt(tsStr, 10);
  if (!Number.isFinite(ts)) return null;

  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > SESSION_TTL_SECONDS) return null;

  const secret = getStateSecret();
  const expected = createHmac("sha256", secret)
    .update(`${tenantId}.${nonce}.${ts}`)
    .digest("hex");

  // timingSafeEqual requires equal-length buffers; if the lengths differ
  // we already know the sigs don't match.
  let expectedBuf: Buffer;
  let sigBuf: Buffer;
  try {
    expectedBuf = Buffer.from(expected, "hex");
    sigBuf = Buffer.from(sig, "hex");
  } catch {
    return null;
  }
  if (expectedBuf.length === 0 || expectedBuf.length !== sigBuf.length) {
    return null;
  }
  if (!timingSafeEqual(expectedBuf, sigBuf)) return null;

  return tenantId;
}

/**
 * Read the session cookie and verify it matches `tenantId`. On any
 * failure, redirect to the onboarding error page (which throws via
 * Next's `redirect()`, so the calling page never sees `null`).
 *
 * Use at the top of every server component under `/onboarding/[tenantId]/*`.
 */
export async function requireSession(
  tenantId: string,
): Promise<{ tenantId: string; token: string }> {
  const store = await cookies();
  const cookie = store.get(SESSION_COOKIE_NAME);
  if (!cookie?.value) {
    redirect("/onboarding/error?reason=no_session");
  }
  const verified = verifySessionToken(cookie.value);
  if (!verified) {
    redirect("/onboarding/error?reason=bad_session");
  }
  if (verified !== tenantId) {
    redirect("/onboarding/error?reason=tenant_mismatch");
  }
  return { tenantId: verified, token: cookie.value };
}
