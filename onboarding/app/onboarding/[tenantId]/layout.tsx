/**
 * Onboarding layout — stripped down to just a header.
 *
 * The onboarding flow is intentionally tiny: two steps, no sidebar.
 *
 *   1. `/integrations` — connect data sources (the whole point)
 *   2. `/done` — confirmation + how to try it in Slack
 *
 * Everything that was formerly a "step" (system prompt, channels,
 * skills, automations) now lives under `/workspace/[tenantId]/*` and
 * is reached via a link on the Done page. New tenants get a fully
 * working bot from the defaults — no forms to fill.
 *
 * Per-page auth (`requireSession(tenantId)`) stays on each page
 * because layout-level redirects cascade and are harder to reason
 * about when `params` is a Promise.
 */
import Link from "next/link";

export default async function OnboardingLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;

  return (
    <div className="flex flex-1 flex-col">
      <header className="border-b border-[color:var(--border)] bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-4">
          <Link href="/" className="font-semibold tracking-tight">
            agent-core
          </Link>
          <span className="font-mono text-xs text-[color:var(--muted)]">
            tenant: {tenantId}
          </span>
        </div>
      </header>
      <main className="mx-auto w-full max-w-3xl flex-1 px-6 py-12">
        {children}
      </main>
    </div>
  );
}
