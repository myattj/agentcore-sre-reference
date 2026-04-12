/**
 * `/ops/login` POST handler.
 *
 * Compares the submitted secret against `ADMIN_SECRET`. On match, sets
 * the `ops_admin_token` HttpOnly cookie (value = the secret itself) and
 * redirects to `/ops`. On mismatch, bounces back to `/ops/login?e=1` so
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

import { getAdminSecret } from "@/lib/env";
import { OPS_COOKIE_NAME, OPS_COOKIE_OPTIONS } from "@/lib/ops";

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
    return redirectTo(request, "/ops/login?e=1");
  }

  const form = await request.formData();
  const submitted = form.get("secret");
  if (typeof submitted !== "string" || !secretsEqual(submitted, expected)) {
    return redirectTo(request, "/ops/login?e=1");
  }

  const response = redirectTo(request, "/ops");
  response.cookies.set({
    name: OPS_COOKIE_NAME,
    value: expected,
    ...OPS_COOKIE_OPTIONS,
  });
  return response;
}

/** Compose an absolute URL from the ALB-forwarded host headers. */
function redirectTo(request: NextRequest, path: string): NextResponse {
  const proto = request.headers.get("x-forwarded-proto") ?? "https";
  const host =
    request.headers.get("x-forwarded-host") ?? request.headers.get("host") ?? "";
  const origin = host ? `${proto}://${host}` : request.url;
  return NextResponse.redirect(new URL(path, origin), { status: 302 });
}
