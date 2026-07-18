/**
 * `/ops/login` POST handler.
 *
 * Compares the submitted secret against `ADMIN_SECRET`. On match, sets
 * an HMAC-signed `ops_session` HttpOnly cookie and redirects to `/ops`.
 * The raw operator credential never enters browser storage. On mismatch,
 * the handler bounces back to `/ops/login?e=1` so
 * the page can show an error banner.
 *
 * This MUST be a Route Handler because cookies cannot be set from a
 * Server Component in Next.js 16 (gotcha #25 / #16).
 *
 * Timing-safe comparison via `crypto.timingSafeEqual`: a naive `===`
 * would leak the matching-prefix length via the HTTP timing channel.
 */
import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";

import { getAdminSecret, getOnboardingPublicOrigin } from "@/lib/env";
import {
  makeOpsSession,
  OPS_COOKIE_NAME,
  OPS_COOKIE_OPTIONS,
} from "@/lib/ops";

function secretsEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, "utf8");
  const bBuf = Buffer.from(b, "utf8");
  if (aBuf.length !== bBuf.length) return false;
  return timingSafeEqual(aBuf, bBuf);
}

export async function POST(request: NextRequest) {
  const expected = getAdminSecret();

  // ADMIN_SECRET unset — ops is disabled. Render the login page's
  // helpful error state by returning a redirect to the page with the
  // error marker. Could also 503 but that's less user-friendly.
  if (!expected) {
    return redirectTo("/ops/login?e=1");
  }

  const form = await request.formData();
  const submitted = form.get("secret");
  if (typeof submitted !== "string" || !secretsEqual(submitted, expected)) {
    return redirectTo("/ops/login?e=1");
  }

  const response = redirectTo("/ops");
  response.cookies.set({
    name: OPS_COOKIE_NAME,
    value: makeOpsSession(expected),
    ...OPS_COOKIE_OPTIONS,
  });
  return response;
}

/** Redirect only through the configured public origin, never request headers. */
function redirectTo(path: string): NextResponse {
  return NextResponse.redirect(new URL(path, getOnboardingPublicOrigin()), {
    status: 302,
  });
}
