/**
 * Client form for editing the tenant's system prompt.
 *
 * Uses debounced auto-save — changes persist ~1.5s after the last edit.
 * No explicit save button needed.
 *
 * Tools and context assembly are intentionally NOT exposed here. Built-in
 * catalog tools are enabled by default, and permalink resolution /
 * thread history injection are both on by default. Users who want to
 * customize either can use ``manage_config`` from an operator-authorized
 * Slack admin account. The goal of this page is to stay as frictionless as
 * possible so most users can accept the default prompt and move on to
 * connecting integrations.
 */
"use client";

import { useCallback, useState } from "react";

import type { TenantConfig, TenantConfigPatch } from "@/lib/types";
import { type AutoSaveStatus, useAutoSave } from "@/lib/useAutoSave";

import { type SaveTenantResult, saveTenantConfig } from "./actions";

type Props = {
  tenantId: string;
  initial: TenantConfig;
};

export default function ConfigForm({ tenantId, initial }: Props) {
  const [systemPrompt, setSystemPrompt] = useState(initial.system_prompt);

  const data: TenantConfigPatch = {
    system_prompt: systemPrompt,
  };

  const save = useCallback(
    async (patch: TenantConfigPatch): Promise<{ ok: boolean; error?: string }> => {
      const result: SaveTenantResult = await saveTenantConfig(tenantId, patch);
      return result.ok ? { ok: true } : { ok: false, error: result.error };
    },
    [tenantId],
  );

  const status = useAutoSave(data, save);

  return (
    <div className="space-y-8">
      <div>
        <label
          htmlFor="system_prompt"
          className="mb-2 block text-sm font-medium"
        >
          System prompt
        </label>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          The personality and instructions the agent uses for every
          reply. Your bot already ships with a comprehensive default
          prompt that knows how to triage alerts, answer questions,
          summarize threads, and do on-call handoffs — only edit this
          if you want to change the bot&apos;s personality or add
          team-specific context.
        </p>
        <textarea
          id="system_prompt"
          name="system_prompt"
          required
          minLength={1}
          rows={16}
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          className="w-full rounded-lg border border-[color:var(--border)] bg-white p-3 font-mono text-xs leading-relaxed shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
        />
      </div>

      <p className="rounded-lg border border-blue-100 bg-blue-50 p-3 text-xs text-blue-800">
        <strong>Good to know:</strong> built-in tools such as team-history
        search, escalation, and cross-channel posting are enabled by default.
        Document tools appear after you connect a Gateway integration. The bot
        also reads Slack thread history + permalinks when relevant. An
        operator-authorized Slack admin can tweak built-in tools by asking
        the bot — e.g.{" "}
        <code className="rounded bg-white px-1 font-mono text-[11px]">
          disable the escalate tool
        </code>
        {" "}or{" "}
        <code className="rounded bg-white px-1 font-mono text-[11px]">
          stop reading thread history
        </code>
        .
      </p>

      <StatusIndicator status={status} />
    </div>
  );
}

function StatusIndicator({ status }: { status: AutoSaveStatus }) {
  if (status.kind === "idle") return null;
  return (
    <div
      aria-live={status.kind === "error" ? "assertive" : "polite"}
      className="text-xs"
      role={status.kind === "error" ? "alert" : "status"}
    >
      {status.kind === "saving" ? (
        <span className="text-[color:var(--muted)]">Saving...</span>
      ) : null}
      {status.kind === "saved" ? (
        <span className="text-green-600">Saved</span>
      ) : null}
      {status.kind === "error" ? (
        <span className="text-red-600">
          Couldn&apos;t save: {status.message}
        </span>
      ) : null}
    </div>
  );
}
