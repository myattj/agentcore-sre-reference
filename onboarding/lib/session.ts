/**
 * Session token verification + cookie helpers.
 *
 * The bridge mints session tokens in `bridge/bridge/slack_oauth.py`
 * (`make_session_token`) at the end of the OAuth callback and writes the
 * HttpOnly cookie directly before redirecting to the onboarding UI. Format:
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
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { getStateSecret } from "./env";

/** Must match `bridge/bridge/slack_oauth.py:_SESSION_TTL_SECONDS`. */
export const SESSION_TTL_SECONDS = 3600;

/** Cookie name for the onboarding session. */
export const SESSION_COOKIE_NAME = "tenant_session";

/** Short-lived CSRF state for the GitHub App redirect. */
const GITHUB_INSTALL_STATE_TTL_SECONDS = 600;
const MAX_CLOCK_SKEW_SECONDS = 30;

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

  if (!/^\d+$/.test(tsStr)) return null;
  const ts = Number.parseInt(tsStr, 10);
  if (!Number.isSafeInteger(ts)) return null;

  const now = Math.floor(Date.now() / 1000);
  if (ts > now + MAX_CLOCK_SKEW_SECONDS || now - ts > SESSION_TTL_SECONDS) {
    return null;
  }

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
 * Mint a purpose-specific GitHub install state without disclosing the session
 * bearer to GitHub or putting it in URL logs. The HMAC is bound to the exact
 * current session token and uses a domain-separation prefix.
 */
export function makeGitHubInstallState(sessionToken: string): string {
  if (!verifySessionToken(sessionToken)) {
    throw new Error("Cannot mint GitHub install state for an invalid session");
  }
  const nonce = randomBytes(16).toString("hex");
  const ts = Math.floor(Date.now() / 1000);
  const signature = createHmac("sha256", getStateSecret())
    .update(`github-install.${sessionToken}.${nonce}.${ts}`)
    .digest("hex");
  return `${nonce}.${ts}.${signature}`;
}

/** Verify that GitHub returned the state minted for this exact session. */
export function verifyGitHubInstallState(
  state: string | undefined | null,
  sessionToken: string,
): boolean {
  if (!state || !verifySessionToken(sessionToken)) return false;
  const parts = state.split(".");
  if (parts.length !== 3) return false;
  const [nonce, tsRaw, signature] = parts;
  if (!nonce || !signature) return false;
  if (!/^\d+$/.test(tsRaw)) return false;
  const ts = Number.parseInt(tsRaw, 10);
  if (!Number.isSafeInteger(ts)) return false;
  const now = Math.floor(Date.now() / 1000);
  if (
    ts > now + MAX_CLOCK_SKEW_SECONDS ||
    now - ts > GITHUB_INSTALL_STATE_TTL_SECONDS
  ) {
    return false;
  }

  const expected = createHmac("sha256", getStateSecret())
    .update(`github-install.${sessionToken}.${nonce}.${ts}`)
    .digest("hex");
  const expectedBuffer = Buffer.from(expected, "hex");
  const signatureBuffer = Buffer.from(signature, "hex");
  return (
    expectedBuffer.length > 0 &&
    expectedBuffer.length === signatureBuffer.length &&
    timingSafeEqual(expectedBuffer, signatureBuffer)
  );
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
