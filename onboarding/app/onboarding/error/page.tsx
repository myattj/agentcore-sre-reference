/**
 * Onboarding error page.
 *
 * The bridge OAuth callback redirects here on any failure path
 * (`/onboarding/error?reason=<slug>`). The session helper in
 * `lib/session.ts` also redirects here when the cookie is missing or
 * invalid. Each `reason` slug maps to a human-readable explanation.
 */
import Link from "next/link";

import { getBridgeInstallUrl } from "@/lib/env";

const REASONS: Record<string, { title: string; body: string }> = {
  invalid_state: {
    title: "Install link expired",
    body: "Your install link expired. Slack consent needs to be completed within 10 minutes of clicking 'Add to Slack'. Please try again.",
  },
  exchange_failed: {
    title: "Slack install failed",
    body: "Slack rejected the install request. This usually means the authorization code expired or the app is misconfigured. Please try again.",
  },
  not_configured: {
    title: "Server not configured",
    body: "The bridge is missing required Slack credentials. Contact support — this is on our side, not yours.",
  },
  missing_fields: {
    title: "Unexpected response from Slack",
    body: "Slack returned an install response we couldn't parse. Please try again or contact support.",
  },
  provisioning_failed: {
    title: "Could not finish install",
    body: "Slack accepted the install but we couldn't write your tenant configuration. Please contact support — your workspace was not charged.",
  },
  no_session: {
    title: "Session required",
    body: "You need to install agent-core in your Slack workspace before you can edit your configuration.",
  },
  bad_session: {
    title: "Session expired",
    body: "Your onboarding session has expired (60-minute window). Please re-run the install to continue.",
  },
  tenant_mismatch: {
    title: "Wrong tenant",
    body: "Your session is for a different workspace than the one you're trying to access.",
  },
};

const DEFAULT_REASON = {
  title: "Something went wrong",
  body: "We hit an unexpected error. Please try again.",
};

type SearchParams = Promise<{ reason?: string }>;

export default async function OnboardingErrorPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const { reason } = await searchParams;
  const message = (reason && REASONS[reason]) || DEFAULT_REASON;
  const installUrl = getBridgeInstallUrl();

  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-16">
      <div className="max-w-xl text-center">
        <p className="mb-4 text-xs font-semibold uppercase tracking-wider text-red-600">
          Install error
        </p>
        <h1 className="mb-4 text-3xl font-semibold tracking-tight">
          {message.title}
        </h1>
        <p className="mb-8 text-[color:var(--muted)]">{message.body}</p>
        <div className="flex flex-col items-center gap-4">
          <a
            href={installUrl}
            className="inline-flex items-center gap-2 rounded-full bg-[color:var(--accent)] px-6 py-3 text-sm font-medium text-white hover:bg-[color:var(--accent-hover)]"
          >
            Try install again
          </a>
          <Link href="/" className="text-sm text-[color:var(--muted)] underline">
            Back to home
          </Link>
        </div>
        {reason ? (
          <p className="mt-12 font-mono text-xs text-[color:var(--muted)]">
            error: {reason}
          </p>
        ) : null}
      </div>
    </main>
  );
}
