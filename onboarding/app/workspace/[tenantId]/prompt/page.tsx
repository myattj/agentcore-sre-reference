/**
 * Prompt settings — the one thing most teams might want to edit: the
 * bot's system prompt. Everything else (tools, context assembly) is
 * intentionally on by default and editable via the bot in Slack.
 *
 * This page lives outside onboarding — new tenants land straight on
 * integrations and never see it. Power users who want to change the
 * personality come back here from `/workspace/{id}`.
 */
import { BridgeApiError, getTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";

import ConfigForm from "./ConfigForm";

export default async function PromptSettingsPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const { token } = await requireSession(tenantId);

  let tenant;
  try {
    tenant = await getTenant(tenantId, token);
  } catch (e) {
    if (e instanceof BridgeApiError && e.status === 404) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Tenant not found</h2>
          <p className="text-sm">
            We couldn&apos;t find your tenant in our database. This usually
            means the OAuth install didn&apos;t complete successfully.
          </p>
        </div>
      );
    }
    throw e;
  }

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">
          System prompt
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          The personality and instructions your bot uses for every reply.
          The default already knows how to triage alerts, answer
          questions, and do on-call handoffs — only edit this if you
          want to change the voice or add team-specific context.
        </p>
      </header>

      <ConfigForm tenantId={tenantId} initial={tenant} />
    </div>
  );
}
