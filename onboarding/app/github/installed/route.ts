/**
 * GitHub App post-install redirect handler.
 *
 * GitHub redirects here after a user completes the Agent GitHub App
 * install on their org:
 *
 *   GET /github/installed?installation_id=XYZ&setup_action=install&state=<nonce>
 *
 * This handler:
 *   1. Verifies the session cookie is present and valid
 *   2. Verifies the purpose-specific `state` is signed and bound to the
 *      current session (the bearer itself is never sent to GitHub)
 *   3. POSTs {installation_id} to the bridge's warm-start endpoint
 *   4. Redirects back to /onboarding/{tenantId}/integrations with a
 *      ?github=connected|error query param for the UI to render a banner
 *
 * On any failure before we know the tenant_id (no cookie, bad cookie,
 * missing state), redirect to /onboarding/error — there's no tenant to
 * redirect "back" to.
 *
 * **Why a Route Handler and not a page**: a page would render HTML before
 * running the bridge POST, which is a wasted round-trip. A route handler
 * lets us do the work server-side and redirect straight to the final URL.
 * Per CLAUDE.md gotcha #16 (Next.js 16 tripwires), Server Components
 * cannot set cookies; since we ONLY read cookies here, either would work,
 * but a route handler is the cleaner shape for "do work, then redirect".
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { NextRequest } from "next/server";

import { BridgeApiError, installGitHubApp } from "@/lib/bridge";
import {
  SESSION_COOKIE_NAME,
  verifyGitHubInstallState,
  verifySessionToken,
} from "@/lib/session";

export async function GET(request: NextRequest): Promise<Response> {
  const searchParams = request.nextUrl.searchParams;
  const installationId = searchParams.get("installation_id");
  const state = searchParams.get("state");
  const setupAction = searchParams.get("setup_action");

  // GitHub installation IDs are positive integers. Reject non-canonical
  // callback input so the bridge trust-binding comparison is unambiguous.
  if (!installationId || !/^[1-9]\d{0,18}$/.test(installationId)) {
    redirect("/onboarding/error?reason=github_missing_installation_id");
  }

  // GitHub sends setup_action=install on fresh install, =update on a
  // subsequent "configure" visit (e.g. adding a repo). Both should
  // trigger warm-start; only a missing value is suspect.
  if (setupAction && setupAction !== "install" && setupAction !== "update") {
    redirect(
      `/onboarding/error?reason=github_unexpected_setup_action_${encodeURIComponent(
        setupAction,
      )}`,
    );
  }

  // Read the session cookie. This is the authoritative source of truth
  // for "which tenant is installing" — we don't trust the state param
  // on its own.
  const cookieStore = await cookies();
  const sessionCookie = cookieStore.get(SESSION_COOKIE_NAME);
  if (!sessionCookie?.value) {
    redirect("/onboarding/error?reason=no_session");
  }

  const tenantId = verifySessionToken(sessionCookie.value);
  if (!tenantId) {
    redirect("/onboarding/error?reason=bad_session");
  }

  // CSRF guard: verify the dedicated short-lived state is bound to this
  // exact session. Never compare or transmit the bearer as URL state.
  if (!verifyGitHubInstallState(state, sessionCookie.value)) {
    redirect(
      `/onboarding/${encodeURIComponent(tenantId)}/integrations?github=error&reason=state_mismatch`,
    );
  }

  // Everything verified. Kick off the warm-start.
  try {
    const result = await installGitHubApp(
      tenantId,
      sessionCookie.value,
      installationId,
    );
    if (result.pending_approval) {
      redirect(
        `/onboarding/${encodeURIComponent(tenantId)}/integrations?github=pending&installation_id=${encodeURIComponent(installationId)}`,
      );
    }
    if (!result.ok) {
      const errCode = encodeURIComponent(result.error ?? "unknown_error");
      redirect(
        `/onboarding/${encodeURIComponent(tenantId)}/integrations?github=error&reason=${errCode}`,
      );
    }
    const repo = result.default_repo ?? "";
    const total = result.total_repos_available;
    redirect(
      `/onboarding/${encodeURIComponent(tenantId)}/integrations?github=connected&repo=${encodeURIComponent(repo)}&total=${total}`,
    );
  } catch (e) {
    // redirect() throws NEXT_REDIRECT — let it propagate. Anything else
    // is a real bridge/network error.
    if (isNextRedirectError(e)) {
      throw e;
    }
    const detail =
      e instanceof BridgeApiError ? e.detail : "unexpected_error";
    redirect(
      `/onboarding/${encodeURIComponent(tenantId)}/integrations?github=error&reason=${encodeURIComponent(detail)}`,
    );
  }
}

/**
 * Detects Next.js's internal ``NEXT_REDIRECT`` sentinel so we don't
 * catch-and-swallow our own ``redirect()`` calls. Per CLAUDE.md
 * gotcha #16: ``redirect()`` throws this error and the runtime
 * expects to see it propagate out of the handler.
 */
function isNextRedirectError(e: unknown): boolean {
  return (
    typeof e === "object" &&
    e !== null &&
    "digest" in e &&
    typeof (e as { digest?: unknown }).digest === "string" &&
    (e as { digest: string }).digest.startsWith("NEXT_REDIRECT")
  );
}
