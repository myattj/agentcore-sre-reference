"use client";

import { useState, useTransition } from "react";

import type { BotPolicyConfig, ChannelInfo } from "@/lib/types";

import { type SaveResult, saveBotPolicy } from "./actions";

type Status =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

type Props = {
  tenantId: string;
  initial: BotPolicyConfig;
  channels: ChannelInfo[];
};

export default function BotPolicyEditor({ tenantId, initial, channels }: Props) {
  // `allow_all_bots` may be missing on legacy tenant rows — default to
  // the new permissive posture when the field isn't set, matching the
  // server-side ``BotPolicyConfigOut`` default.
  const [allowAllBots, setAllowAllBots] = useState<boolean>(initial.allow_all_bots ?? true);
  const [trustedBotIds, setTrustedBotIds] = useState<string[]>(initial.trusted_bot_ids);
  const [openChannels, setOpenChannels] = useState<string[]>(initial.open_channels);
  const [newBotId, setNewBotId] = useState("");
  const [selectedChannel, setSelectedChannel] = useState("");
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [isPending, startTransition] = useTransition();

  function toggleAllowAllBots(next: boolean) {
    setAllowAllBots(next);
    persist(next, trustedBotIds, openChannels);
  }

  function addBot() {
    const id = newBotId.trim();
    if (!id || trustedBotIds.includes(id)) return;
    const updated = [...trustedBotIds, id];
    setTrustedBotIds(updated);
    setNewBotId("");
    persist(allowAllBots, updated, openChannels);
  }

  function removeBot(id: string) {
    const updated = trustedBotIds.filter((b) => b !== id);
    setTrustedBotIds(updated);
    persist(allowAllBots, updated, openChannels);
  }

  function addChannel() {
    const id = selectedChannel.trim();
    if (!id || openChannels.includes(id)) return;
    const updated = [...openChannels, id];
    setOpenChannels(updated);
    setSelectedChannel("");
    persist(allowAllBots, trustedBotIds, updated);
  }

  function removeChannel(id: string) {
    const updated = openChannels.filter((c) => c !== id);
    setOpenChannels(updated);
    persist(allowAllBots, trustedBotIds, updated);
  }

  function persist(allowAll: boolean, bots: string[], chans: string[]) {
    setStatus({ kind: "pending" });
    startTransition(async () => {
      const result: SaveResult = await saveBotPolicy(tenantId, {
        allow_all_bots: allowAll,
        trusted_bot_ids: bots,
        open_channels: chans,
      });
      if (result.ok) {
        setStatus({ kind: "saved" });
      } else {
        setStatus({ kind: "error", message: result.error });
      }
    });
  }

  const channelName = (id: string) => {
    const ch = channels.find((c) => c.id === id);
    return ch ? `${ch.is_private ? "* " : "# "}${ch.name}` : id;
  };

  return (
    <div className="space-y-6">
      <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-4 transition hover:border-[color:var(--accent)]/40">
        <input
          type="checkbox"
          checked={allowAllBots}
          disabled={isPending}
          onChange={(e) => toggleAllowAllBots(e.target.checked)}
          className="mt-1 h-4 w-4 cursor-pointer rounded border-[color:var(--border)] text-[color:var(--accent)] focus:ring-[color:var(--accent)]"
        />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">Allow all bots (default)</div>
          <div className="text-xs text-[color:var(--muted)]">
            PagerDuty, Datadog, and any other bot can @mention your
            agent to trigger auto-triage. Turn this off to use the
            granular trusted-bot / open-channel whitelist below.
          </div>
        </div>
      </label>

      {allowAllBots ? (
        <p className="text-xs italic text-[color:var(--muted)]">
          All bots are currently allowed — no whitelist needed. Your
          agent&apos;s own messages are still filtered to prevent loops.
        </p>
      ) : null}

      <div className={allowAllBots ? "pointer-events-none opacity-40" : ""}>
      <div>
        <h3 className="mb-1 text-sm font-medium">Trusted bots</h3>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          Bots in this list can always trigger your agent by @mentioning it.
        </p>
        {trustedBotIds.length > 0 ? (
          <ul className="mb-3 divide-y divide-[color:var(--border)] rounded-lg border border-[color:var(--border)]">
            {trustedBotIds.map((id) => (
              <li key={id} className="flex items-center justify-between px-4 py-2.5">
                <code className="font-mono text-sm">{id}</code>
                <button
                  type="button"
                  onClick={() => removeBot(id)}
                  disabled={isPending}
                  className="text-xs text-red-600 hover:text-red-700 disabled:opacity-40"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="flex gap-2">
          <input
            type="text"
            value={newBotId}
            onChange={(e) => setNewBotId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addBot())}
            placeholder="B0123456789"
            className="flex-1 rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
          />
          <button
            type="button"
            onClick={addBot}
            disabled={!newBotId.trim() || isPending}
            className="rounded-full border border-[color:var(--border)] px-4 py-2 text-sm font-medium hover:bg-[color:var(--card)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Add
          </button>
        </div>
        <p className="mt-1.5 text-[10px] text-[color:var(--muted)]">
          Find bot IDs in Slack&apos;s admin panel or by right-clicking a bot&apos;s
          profile.
        </p>
      </div>

      <div>
        <h3 className="mb-1 text-sm font-medium">Open channels</h3>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          Any bot can trigger your agent in these channels (useful for alert channels).
        </p>
        {openChannels.length > 0 ? (
          <ul className="mb-3 divide-y divide-[color:var(--border)] rounded-lg border border-[color:var(--border)]">
            {openChannels.map((id) => (
              <li key={id} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-sm">{channelName(id)}</span>
                <button
                  type="button"
                  onClick={() => removeChannel(id)}
                  disabled={isPending}
                  className="text-xs text-red-600 hover:text-red-700 disabled:opacity-40"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="flex gap-2">
          {channels.length > 0 ? (
            <select
              value={selectedChannel}
              onChange={(e) => setSelectedChannel(e.target.value)}
              className="flex-1 rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
            >
              <option value="">Select a channel...</option>
              {channels
                .filter((ch) => !openChannels.includes(ch.id))
                .map((ch) => (
                  <option key={ch.id} value={ch.id}>
                    {ch.is_private ? "* " : "# "}
                    {ch.name}
                  </option>
                ))}
            </select>
          ) : (
            <input
              type="text"
              value={selectedChannel}
              onChange={(e) => setSelectedChannel(e.target.value)}
              onKeyDown={(e) =>
                e.key === "Enter" && (e.preventDefault(), addChannel())
              }
              placeholder="C0123456789"
              className="flex-1 rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
            />
          )}
          <button
            type="button"
            onClick={addChannel}
            disabled={!selectedChannel.trim() || isPending}
            className="rounded-full border border-[color:var(--border)] px-4 py-2 text-sm font-medium hover:bg-[color:var(--card)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Add
          </button>
        </div>
      </div>

      </div>

      {!allowAllBots ? (
        <p className="rounded-lg border border-blue-100 bg-blue-50 p-3 text-xs text-blue-800">
          Bots not in the trusted list are blocked outside of open
          channels. Your agent&apos;s own messages are always filtered
          to prevent loops.
        </p>
      ) : null}

      {status.kind === "saved" ? (
        <span className="text-sm text-green-600">Saved.</span>
      ) : null}
      {status.kind === "error" ? (
        <span className="text-sm text-red-600">
          Couldn&apos;t save: {status.message}
        </span>
      ) : null}
    </div>
  );
}
