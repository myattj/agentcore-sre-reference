/**
 * Layout for the tenant onboarding flow.
 *
 * Renders a sidebar with the four steps (Config → Channels →
 * Integrations → Done) and slots the page content next to it. The
 * session check is NOT in the layout — each page calls
 * `requireSession(tenantId)` itself so the redirect-on-failure path is
 * obvious. (Layout-level auth is harder to reason about because
 * `params` is a Promise and an early redirect from a layout cascades
 * through every nested page.)
 */
import Link from "next/link";

const STEPS = [
  { slug: "config", label: "1. Configure", description: "System prompt + tools" },
  { slug: "channels", label: "2. Channels", description: "Where the bot listens" },
  {
    slug: "integrations",
    label: "3. Integrations",
    description: "Connect data sources",
  },
  { slug: "done", label: "4. Done", description: "Try it in Slack" },
];

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
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link href="/" className="font-semibold tracking-tight">
            agent-core
          </Link>
          <span className="font-mono text-xs text-[color:var(--muted)]">
            tenant: {tenantId}
          </span>
        </div>
      </header>
      <div className="mx-auto flex w-full max-w-6xl flex-1 gap-12 px-6 py-12">
        <nav className="w-64 shrink-0">
          <p className="mb-4 text-xs font-semibold uppercase tracking-wider text-[color:var(--muted)]">
            Onboarding
          </p>
          <ul className="space-y-1">
            {STEPS.map((step) => (
              <li key={step.slug}>
                <Link
                  href={`/onboarding/${encodeURIComponent(tenantId)}/${step.slug}`}
                  className="block rounded-md px-3 py-2 text-sm transition hover:bg-[color:var(--card)]"
                >
                  <div className="font-medium">{step.label}</div>
                  <div className="text-xs text-[color:var(--muted)]">
                    {step.description}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}
