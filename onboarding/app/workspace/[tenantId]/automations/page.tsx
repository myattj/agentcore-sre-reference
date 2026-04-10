import { BridgeApiError, getTenant, listChannels } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { ChannelInfo } from "@/lib/types";

import BotPolicyEditor from "./BotPolicyEditor";
import EscalationEditor from "./EscalationEditor";

export default async function AutomationsPage({
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
            We couldn&apos;t find your tenant in our database.
          </p>
        </div>
      );
    }
    throw e;
  }

  let channels: ChannelInfo[] = [];
  try {
    const resp = await listChannels(tenantId, token);
    channels = resp.channels;
  } catch {
    // Channel listing is best-effort; forms fall back to free-text input.
  }

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">
          Automations
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Control bot-to-bot interactions and set up escalation routing.
        </p>
      </header>

      <div className="mb-8 rounded-lg border border-violet-100 bg-violet-50 p-4 text-sm text-violet-800">
        <p className="mb-1 font-medium">These settings are always editable</p>
        <p className="text-xs text-violet-700">
          You can also update bot policy, escalation routes, and skills at any
          time by asking your bot directly in Slack. Just say something like
          &ldquo;add B_ALERTBOT to trusted bots&rdquo; or &ldquo;add an
          escalation route for the security team.&rdquo; Changes persist
          immediately and the bot will remember them.
        </p>
      </div>

      <section className="mb-10">
        <h2 className="mb-1 text-lg font-semibold">Bot-to-Bot Policy</h2>
        <p className="mb-4 text-xs text-[color:var(--muted)]">
          Control which Slack bots can trigger your agent.
        </p>
        <BotPolicyEditor
          tenantId={tenantId}
          initial={tenant.bot_policy}
          channels={channels}
        />
      </section>

      <hr className="border-[color:var(--border)]" />

      <section className="mt-10">
        <h2 className="mb-1 text-lg font-semibold">Escalation Routing</h2>
        <p className="mb-4 text-xs text-[color:var(--muted)]">
          Define teams and where to route escalations. The{" "}
          <code className="rounded bg-[color:var(--card)] px-1 font-mono text-[10px]">
            escalate
          </code>{" "}
          tool uses this routing table.
        </p>
        <EscalationEditor
          tenantId={tenantId}
          initial={tenant.escalation.routes}
          channels={channels}
        />
      </section>
    </div>
  );
}
