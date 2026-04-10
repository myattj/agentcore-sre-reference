/**
 * Integrations page — all 7 connectors are live (week 5).
 *
 * Each integration card is an interactive client component that calls a
 * server action -> bridge -> gateway_provisioner. Per-integration
 * connection status is tracked via `byo.connected_integrations[]`.
 */
import Link from "next/link";

import { getTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";

import { ConfluenceForm } from "./ConfluenceForm";
import { DatadogForm } from "./DatadogForm";
import { GitHubForm } from "./GitHubForm";
import { JiraForm } from "./JiraForm";
import { LinearForm } from "./LinearForm";
import { NotionForm } from "./NotionForm";
import { PagerDutyForm } from "./PagerDutyForm";

export default async function IntegrationsPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const { token } = await requireSession(tenantId);

  let connected: string[] = [];
  try {
    const config = await getTenant(tenantId, token);
    connected = config.byo?.connected_integrations ?? [];
  } catch {
    // If the tenant fetch fails, show all forms (worst case: they
    // reconnect, which is idempotent).
  }

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">
          Integrations
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Connect the data sources the agent uses to answer questions and
          triage alerts.
        </p>
      </header>

      <ul className="grid gap-4 sm:grid-cols-2">
        <li><DatadogForm tenantId={tenantId} initialConnected={connected.includes("datadog")} /></li>
        <li><PagerDutyForm tenantId={tenantId} initialConnected={connected.includes("pagerduty")} /></li>
        <li><GitHubForm tenantId={tenantId} initialConnected={connected.includes("github")} /></li>
        <li><ConfluenceForm tenantId={tenantId} initialConnected={connected.includes("confluence")} /></li>
        <li><NotionForm tenantId={tenantId} initialConnected={connected.includes("notion")} /></li>
        <li><JiraForm tenantId={tenantId} initialConnected={connected.includes("jira")} /></li>
        <li><LinearForm tenantId={tenantId} initialConnected={connected.includes("linear")} /></li>
      </ul>

      <footer className="mt-12 flex items-center justify-between border-t border-[color:var(--border)] pt-6">
        <Link
          href={`/onboarding/${encodeURIComponent(tenantId)}/channels`}
          className="text-sm text-[color:var(--muted)] hover:text-[color:var(--foreground)]"
        >
          &larr; Back to channels
        </Link>
        <Link
          href={`/onboarding/${encodeURIComponent(tenantId)}/done`}
          className="rounded-full bg-[color:var(--accent)] px-5 py-2 text-sm font-medium text-white hover:bg-[color:var(--accent-hover)]"
        >
          Continue &rarr;
        </Link>
      </footer>
    </div>
  );
}
