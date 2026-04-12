/**
 * Workspace settings layout.
 *
 * This is the post-onboarding home for a tenant — where the dev/IT
 * owner comes back to tune things that aren't editable via the bot in
 * Slack. The onboarding flow itself (two steps: integrations → done)
 * lives under `/onboarding/[tenantId]/*` and intentionally does NOT
 * expose any of these pages — the goal there is to get to a working
 * bot with zero typing.
 *
 * Per-page auth (`requireSession(tenantId)`) is kept on each page
 * rather than in the layout, for the same reason onboarding does it:
 * layout-level redirects cascade through every nested page and are
 * harder to reason about.
 */
import Link from "next/link";

const SETTINGS_NAV = [
  { slug: "", label: "Overview", description: "Current setup at a glance" },
  { slug: "prompt", label: "Prompt", description: "Agent personality" },
  { slug: "channels", label: "Channels", description: "Per-channel overrides" },
  { slug: "skills", label: "Skills", description: "Custom runbooks" },
  { slug: "automations", label: "Automations", description: "Bot policy & escalation" },
  { slug: "metrics", label: "Metrics", description: "Usage, errors, cost" },
];

export default async function WorkspaceLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const base = `/workspace/${encodeURIComponent(tenantId)}`;

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
        <nav className="w-56 shrink-0">
          <p className="mb-4 text-xs font-semibold uppercase tracking-wider text-[color:var(--muted)]">
            Settings
          </p>
          <ul className="space-y-1">
            {SETTINGS_NAV.map((item) => (
              <li key={item.slug || "overview"}>
                <Link
                  href={item.slug ? `${base}/${item.slug}` : base}
                  className="block rounded-md px-3 py-2 text-sm transition hover:bg-[color:var(--card)]"
                >
                  <div className="font-medium">{item.label}</div>
                  <div className="text-xs text-[color:var(--muted)]">
                    {item.description}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          <p className="mt-6 px-3 text-[10px] leading-relaxed text-[color:var(--muted)]">
            Most of this is also editable by talking to the bot in
            Slack — it can update its own config.
          </p>
        </nav>
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}
