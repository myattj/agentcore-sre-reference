/**
 * Welcome route handler — entry point from the bridge OAuth callback.
 *
 * The bridge mints a session token and redirects to:
 *   /onboarding/<tenant_id>/welcome?t=<token>
 *
 * This handler:
 *   1. Validates `t` is a real session token for `tenant_id`
 *   2. Sets the HttpOnly `tenant_session` cookie on the onboarding origin
 *   3. Redirects to the clean `/onboarding/<tenant_id>/config` URL
 *      (so the token doesn't sit in browser history / referer headers)
 *
 * This MUST be a Route Handler, not a Server Component page — Next.js 16
 * docs: "Setting cookies is not supported during Server Component
 * rendering." Cookies can only be modified inside Server Functions or
 * Route Handlers. See CLAUDE.md gotcha #25.
 */
import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE_NAME, SESSION_COOKIE_OPTIONS, verifySessionToken } from "@/lib/session";

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ tenantId: string }> },
) {
  const { tenantId } = await context.params;
  const token = request.nextUrl.searchParams.get("t") ?? "";

  // Derive the public origin from the X-Forwarded-* headers set by the ALB,
  // falling back to request.url for local dev. Without this, Next.js uses
  // the internal Fargate hostname (ip-10-0-0-*.compute.internal) as the
  // redirect base, which the browser can't resolve.
  const proto = request.headers.get("x-forwarded-proto") ?? "https";
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host") ?? "";
  const origin = host ? `${proto}://${host}` : request.url;

  const verified = verifySessionToken(token);
  if (!verified) {
    return NextResponse.redirect(
      new URL("/onboarding/error?reason=bad_session", origin),
      { status: 302 },
    );
  }
  if (verified !== tenantId) {
    return NextResponse.redirect(
      new URL("/onboarding/error?reason=tenant_mismatch", origin),
      { status: 302 },
    );
  }

  const response = NextResponse.redirect(
    new URL(`/onboarding/${encodeURIComponent(tenantId)}/config`, origin),
    { status: 302 },
  );
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: token,
    ...SESSION_COOKIE_OPTIONS,
  });
  return response;
}
