/**
 * Channels page — lists Slack channels the bot is in + per-channel
 * persona editor (system prompt, tools, memory rules per channel).
 *
 * Calls `GET /api/tenants/{id}/channels` on the bridge for the channel
 * list, and `GET /api/tenants/{id}` for the current tenant config
 * (including any existing channel personas). The ChannelTabs client
 * component provides a tab per channel with inline editing.
 */
import { BridgeApiError, getTenant, listChannels } from "@/lib/bridge";
import { getBridgeInstallUrl } from "@/lib/env";
import { requireSession } from "@/lib/session";
import type { TenantConfig } from "@/lib/types";

import ChannelTabs from "./ChannelTabs";

export default async function ChannelsPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const { token } = await requireSession(tenantId);

  let channels: { id: string; name: string; is_private: boolean }[] = [];
  let errorMessage: string | null = null;
  let needsReinstall = false;
  let config: TenantConfig | null = null;

  try {
    const [channelResponse, tenantConfig] = await Promise.all([
      listChannels(tenantId, token),
      getTenant(tenantId, token),
    ]);
    channels = channelResponse.channels;
    needsReinstall = Boolean(channelResponse.needs_reinstall);
    config = tenantConfig;
  } catch (e) {
    if (e instanceof BridgeApiError) {
      errorMessage = `${e.status}: ${e.detail}`;
    } else {
      errorMessage = "unexpected error";
    }
  }

  const installUrl = getBridgeInstallUrl();

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">Channels</h1>
        <p className="text-sm text-[color:var(--muted)]">
          By default the bot works in any channel it&apos;s invited to and keeps
          each channel&apos;s memory separate. Use this page to
          override the prompt, tools, or what gets remembered for a
          specific channel — e.g. a strict persona for #sre-alerts, or
          a narrower toolset for #ask-data. Most teams don&apos;t need
          this on day one.
        </p>
      </header>

      {needsReinstall ? (
        <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <p className="mb-2 font-semibold">Re-install needed for channel listing</p>
          <p className="mb-3">
            Your install is from an older version of the app that didn&apos;t
            request the <code className="rounded bg-white px-1 font-mono text-xs">channels:read</code>{" "}
            and{" "}
            <code className="rounded bg-white px-1 font-mono text-xs">groups:read</code>{" "}
            scopes. Re-install in Slack to grant the new permissions — your
            existing config and bot token will be preserved.
          </p>
          <a
            href={installUrl}
            className="inline-flex items-center gap-2 rounded-full bg-amber-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-amber-700"
          >
            Re-install in Slack
          </a>
        </div>
      ) : null}

      {errorMessage ? (
        <div className="mb-6 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-900">
          Couldn&apos;t load channels: {errorMessage}
        </div>
      ) : null}

      {!errorMessage && !needsReinstall && channels.length === 0 ? (
        <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-8 text-center">
          <h2 className="mb-2 text-base font-semibold">No channels yet</h2>
          <p className="mx-auto max-w-md text-sm text-[color:var(--muted)]">
            The bot hasn&apos;t been invited to any channels in your Slack
            workspace. Open Slack, type{" "}
            <code className="rounded bg-white px-1.5 py-0.5 font-mono text-xs">
              /invite @Agent
            </code>{" "}
            in any channel, then come back and refresh.
          </p>
        </div>
      ) : null}

      {channels.length > 0 && config ? (
        <ChannelTabs
          tenantId={tenantId}
          channels={channels}
          config={config}
        />
      ) : null}
    </div>
  );
}
