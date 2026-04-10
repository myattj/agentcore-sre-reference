/**
 * Workspace overview — landing page for post-onboarding settings.
 *
 * Summarizes the tenant's current setup and links into each settings
 * section. This was the "Review" page during onboarding; it now lives
 * as the /workspace/{id} index because it's the natural home for
 * someone returning to tune things.
 */
import Link from "next/link";

import { BridgeApiError, getTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { TenantConfig } from "@/lib/types";

export default async function WorkspaceOverviewPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const { token } = await requireSession(tenantId);

  let tenant: TenantConfig;
  try {
    tenant = await getTenant(tenantId, token);
  } catch (e) {
    if (e instanceof BridgeApiError && e.status === 404) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Tenant not found</h2>
          <p className="text-sm">
            We couldn&apos;t find your tenant in our database.
          </p>
        </div>
      );
    }
    throw e;
  }

  const ca = tenant.context_assembly;
  const toolCount = tenant.catalog.allowed_tools.length;
  const channelCount = Object.keys(tenant.channels).length;
  const skillCount = tenant.skills.length;
  const botCount = tenant.bot_policy.trusted_bot_ids.length;
  const openCount = tenant.bot_policy.open_channels.length;
  const allowAllBots = tenant.bot_policy.allow_all_bots;
  const routeCount = tenant.escalation.routes.length;
  const connected = tenant.byo.connected_integrations ?? [];
  const isolatedChannels = tenant.memory.isolated_channels ?? [];

  const contextParts: string[] = [];
  if (ca.resolve_permalinks) contextParts.push("permalinks");
  if (ca.inject_thread_history) contextParts.push(`thread history (${ca.thread_history_depth} msgs)`);

  // Memory summary line: shared-by-default with isolation callouts.
  const memorySummary =
    isolatedChannels.length === 0
      ? "Shared memory across all channels"
      : `Shared memory, except ${isolatedChannels.length} isolated channel${isolatedChannels.length === 1 ? "" : "s"}`;

  // Bot policy summary line: highlights the "magical default" when on.
  const botPolicySummary = allowAllBots
    ? "All bots allowed (PagerDuty, Datadog, etc. trigger auto-triage)"
    : botCount > 0 || openCount > 0
      ? `${botCount} trusted bot${botCount === 1 ? "" : "s"} \u00b7 ${openCount} open channel${openCount === 1 ? "" : "s"}`
      : "Humans only (bots blocked)";

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">
          Workspace overview
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Your bot&apos;s current setup. Click any section to change it — or
          just ask the bot in Slack.
        </p>
      </header>

      <div className="space-y-4">
        <OverviewSection
          href={`/workspace/${encodeURIComponent(tenantId)}/prompt`}
          title="Prompt"
          items={[
            {
              configured: tenant.system_prompt.length > 0,
              label: tenant.system_prompt.length > 0
                ? `System prompt: "${tenant.system_prompt.slice(0, 60)}${tenant.system_prompt.length > 60 ? "..." : ""}"`
                : "No system prompt set",
            },
            {
              configured: toolCount > 0,
              label: toolCount > 0
                ? `${toolCount} tool${toolCount === 1 ? "" : "s"} enabled`
                : "No tools enabled",
            },
            {
              configured: contextParts.length > 0,
              label: contextParts.length > 0
                ? `Context assembly: ${contextParts.join(" + ")}`
                : "Context assembly disabled",
            },
          ]}
        />

        <OverviewSection
          href={`/workspace/${encodeURIComponent(tenantId)}/channels`}
          title="Channels & memory"
          items={[
            {
              configured: true,
              label: memorySummary,
            },
            {
              configured: channelCount > 0,
              label: channelCount > 0
                ? `${channelCount} channel${channelCount === 1 ? "" : "s"} with custom personas`
                : "No channel-specific personas",
            },
          ]}
        />

        <OverviewSection
          href={`/workspace/${encodeURIComponent(tenantId)}/skills`}
          title="Skills"
          items={[
            {
              configured: skillCount > 0,
              label: skillCount > 0
                ? `${skillCount} skill${skillCount === 1 ? "" : "s"} defined: ${tenant.skills.map((s) => s.trigger).join(" \u00b7 ")}`
                : "No skills defined",
            },
          ]}
        />

        <OverviewSection
          href={`/workspace/${encodeURIComponent(tenantId)}/automations`}
          title="Automations"
          items={[
            {
              configured: allowAllBots || botCount > 0 || openCount > 0,
              label: botPolicySummary,
            },
            {
              configured: routeCount > 0,
              label: routeCount > 0
                ? `${routeCount} escalation route${routeCount === 1 ? "" : "s"} (${tenant.escalation.routes.map((r) => r.team_name).join(", ")})`
                : "No escalation routes",
            },
          ]}
        />

        <OverviewSection
          href={`/onboarding/${encodeURIComponent(tenantId)}/integrations`}
          title="Integrations"
          items={[
            {
              configured: connected.length > 0,
              label: connected.length > 0
                ? `${connected.length} integration${connected.length === 1 ? "" : "s"} connected: ${connected.join(", ")}`
                : "No integrations connected",
            },
          ]}
        />
      </div>
    </div>
  );
}

function OverviewSection({
  href,
  title,
  items,
}: {
  href: string;
  title: string;
  items: { configured: boolean; label: string }[];
}) {
  return (
    <Link
      href={href}
      className="block rounded-lg border border-[color:var(--border)] p-5 transition hover:border-[color:var(--accent)]/40 hover:bg-[color:var(--card)]"
    >
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      <ul className="space-y-1">
        {items.map((item) => (
          <li key={item.label} className="flex items-start gap-2 text-sm">
            <span className={item.configured ? "text-green-600" : "text-[color:var(--muted)]"}>
              {item.configured ? "\u2705" : "\u2B1C"}
            </span>
            <span className="text-[color:var(--muted)]">{item.label}</span>
          </li>
        ))}
      </ul>
    </Link>
  );
}
