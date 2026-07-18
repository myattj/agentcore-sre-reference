/**
 * Per-channel persona editor — tab per channel, each with system prompt
 * textarea, tool checkboxes, and memory rule toggles.
 *
 * The tab panel shows inherited (tenant-level) defaults as placeholder
 * text/state. When a field is explicitly set, it overrides the tenant
 * default for that channel only.
 */
"use client";

import { useCallback, useState } from "react";

import {
  KNOWN_CATALOG_TOOLS,
  type ChannelInfo,
  type ChannelPersona,
  type TenantConfig,
} from "@/lib/types";
import { type AutoSaveStatus, useAutoSave } from "@/lib/useAutoSave";

import { type SaveChannelResult, saveChannelPersona } from "./actions";

const KNOWN_MEMORY_RULES = [
  { id: "user_preferences", label: "User preferences", description: "Remember stated preferences (\"I prefer...\", \"I like...\")." },
  { id: "facts", label: "Facts", description: "Extract factual statements from conversation." },
  { id: "faq_in_channel", label: "FAQ in channel", description: "Store every Q&A pair as an FAQ for this channel." },
];

type Props = {
  tenantId: string;
  channels: ChannelInfo[];
  config: TenantConfig;
};

export default function ChannelTabs({ tenantId, channels, config }: Props) {
  const [activeTab, setActiveTab] = useState<string>(channels[0]?.id ?? "");

  if (channels.length === 0) return null;

  function tabId(channelId: string) {
    return `channel-tab-${channelId}`;
  }

  function panelId(channelId: string) {
    return `channel-panel-${channelId}`;
  }

  function activateTab(index: number) {
    const channel = channels[index];
    if (!channel) return;
    setActiveTab(channel.id);
    document.getElementById(tabId(channel.id))?.focus();
  }

  function handleTabKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % channels.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + channels.length) % channels.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = channels.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    activateTab(nextIndex);
  }

  return (
    <div className="rounded-lg border border-[color:var(--border)]">
      {/* Tab bar */}
      <div
        aria-label="Slack channels"
        className="flex overflow-x-auto border-b border-[color:var(--border)] bg-[color:var(--card)]"
        role="tablist"
      >
        {channels.map((ch, index) => {
          const active = ch.id === activeTab;
          return (
            <button
              key={ch.id}
              aria-controls={panelId(ch.id)}
              aria-selected={active}
              id={tabId(ch.id)}
              role="tab"
              tabIndex={active ? 0 : -1}
              type="button"
              onClick={() => setActiveTab(ch.id)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
              className={`shrink-0 border-b-2 px-4 py-2.5 text-sm font-medium transition ${
                active
                  ? "border-[color:var(--accent)] text-[color:var(--accent)]"
                  : "border-transparent text-[color:var(--muted)] hover:text-[color:var(--foreground)]"
              }`}
            >
              {ch.is_private ? "🔒 " : "# "}
              {ch.name}
            </button>
          );
        })}
      </div>

      {/* Tab panels */}
      {channels.map((ch) =>
        ch.id === activeTab ? (
          <div
            key={ch.id}
            aria-labelledby={tabId(ch.id)}
            id={panelId(ch.id)}
            role="tabpanel"
            tabIndex={0}
          >
            <ChannelPanel
              tenantId={tenantId}
              channelId={ch.id}
              channelName={ch.name}
              persona={config.channels?.[ch.id] ?? {}}
              baseConfig={config}
            />
          </div>
        ) : null,
      )}
    </div>
  );
}

function ChannelPanel({
  tenantId,
  channelId,
  channelName,
  persona,
  baseConfig,
}: {
  tenantId: string;
  channelId: string;
  channelName: string;
  persona: ChannelPersona;
  baseConfig: TenantConfig;
}) {
  const [systemPrompt, setSystemPrompt] = useState(persona.system_prompt ?? "");
  const [useCustomPrompt, setUseCustomPrompt] = useState(persona.system_prompt != null);
  const [allowedTools, setAllowedTools] = useState<string[]>(
    persona.allowed_tools ?? baseConfig.catalog.allowed_tools,
  );
  const [useCustomTools, setUseCustomTools] = useState(persona.allowed_tools != null);
  const [memoryRules, setMemoryRules] = useState<string[]>(
    persona.memory_rules ?? baseConfig.memory.extraction.rules,
  );
  const [useCustomRules, setUseCustomRules] = useState(persona.memory_rules != null);

  const data: ChannelPersona = {
    system_prompt: useCustomPrompt ? systemPrompt : null,
    allowed_tools: useCustomTools ? allowedTools : null,
    memory_rules: useCustomRules ? memoryRules : null,
  };

  const save = useCallback(
    async (patch: ChannelPersona): Promise<{ ok: boolean; error?: string }> => {
      const result: SaveChannelResult = await saveChannelPersona(
        tenantId,
        channelId,
        patch,
      );
      return result.ok ? { ok: true } : { ok: false, error: result.error };
    },
    [tenantId, channelId],
  );

  const status = useAutoSave(data, save);

  function toggleTool(toolId: string) {
    setAllowedTools((current) =>
      current.includes(toolId)
        ? current.filter((t) => t !== toolId)
        : [...current, toolId],
    );
  }

  function toggleRule(ruleId: string) {
    setMemoryRules((current) =>
      current.includes(ruleId)
        ? current.filter((r) => r !== ruleId)
        : [...current, ruleId],
    );
  }

  return (
    <div className="space-y-6 p-5">
      <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-[color:var(--muted)]">
          Overrides for <strong>#{channelName}</strong> ({channelId}). Leave a
          section unchecked to inherit from the tenant-level defaults.
        </p>
        <StatusIndicator status={status} />
      </div>

      {/* System prompt override */}
      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={useCustomPrompt}
            onChange={(e) => {
              setUseCustomPrompt(e.target.checked);
              if (!e.target.checked) setSystemPrompt("");
            }}
            className="h-4 w-4 rounded border-[color:var(--border)] text-[color:var(--accent)]"
          />
          Custom system prompt
        </label>
        {useCustomPrompt ? (
          <>
            <textarea
              rows={4}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={"You are the SRE on-call assistant for #" + channelName + ". When someone says /oncall-start, search this channel for active incidents and summarize with action items."}
              className="w-full rounded-lg border border-[color:var(--border)] bg-white p-3 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
            />
            <p className="text-xs text-[color:var(--muted)]">
              Define this channel&apos;s personality, team-specific workflows,
              and behavioral rules. This replaces the tenant-level prompt for
              messages in this channel.
            </p>
          </>
        ) : (
          <p className="text-xs italic text-[color:var(--muted)]">
            Using tenant default: &ldquo;{baseConfig.system_prompt.slice(0, 80)}
            {baseConfig.system_prompt.length > 80 ? "..." : ""}&rdquo;
          </p>
        )}
      </div>

      {/* Tool override */}
      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={useCustomTools}
            onChange={(e) => {
              setUseCustomTools(e.target.checked);
              if (!e.target.checked) {
                setAllowedTools(baseConfig.catalog.allowed_tools);
              }
            }}
            className="h-4 w-4 rounded border-[color:var(--border)] text-[color:var(--accent)]"
          />
          Custom tool set
        </label>
        {useCustomTools ? (
          <ul className="space-y-1.5">
            {KNOWN_CATALOG_TOOLS.map((tool) => (
              <li key={tool.id}>
                <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-2.5 transition hover:border-[color:var(--accent)]/40">
                  <input
                    type="checkbox"
                    checked={allowedTools.includes(tool.id)}
                    onChange={() => toggleTool(tool.id)}
                    className="mt-0.5 h-4 w-4 rounded border-[color:var(--border)] text-[color:var(--accent)]"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{tool.label}</div>
                    <div className="text-xs text-[color:var(--muted)]">{tool.description}</div>
                  </div>
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs italic text-[color:var(--muted)]">
            Using tenant default: {baseConfig.catalog.allowed_tools.join(", ") || "none"}
          </p>
        )}
      </div>

      {/* Memory rules override — what to remember in this channel.
          Note: this controls *what* the agent extracts, not *where* it
          stores the memory. All channels share one memory brain by
          default; isolation lives on tenant-level ``memory.isolated_channels``
          and is set via the bot's ``manage_config`` tool. */}
      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={useCustomRules}
            onChange={(e) => {
              setUseCustomRules(e.target.checked);
              if (!e.target.checked) {
                setMemoryRules(baseConfig.memory.extraction.rules);
              }
            }}
            className="h-4 w-4 rounded border-[color:var(--border)] text-[color:var(--accent)]"
          />
          Custom: what to remember in this channel
        </label>
        {useCustomRules ? (
          <ul className="space-y-1.5">
            {KNOWN_MEMORY_RULES.map((rule) => (
              <li key={rule.id}>
                <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-2.5 transition hover:border-[color:var(--accent)]/40">
                  <input
                    type="checkbox"
                    checked={memoryRules.includes(rule.id)}
                    onChange={() => toggleRule(rule.id)}
                    className="mt-0.5 h-4 w-4 rounded border-[color:var(--border)] text-[color:var(--accent)]"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{rule.label}</div>
                    <div className="text-xs text-[color:var(--muted)]">{rule.description}</div>
                  </div>
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs italic text-[color:var(--muted)]">
            Using tenant default: {baseConfig.memory.extraction.rules.join(", ") || "none"}
          </p>
        )}
      </div>
    </div>
  );
}

function StatusIndicator({ status }: { status: AutoSaveStatus }) {
  if (status.kind === "idle") return null;
  return (
    <span
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
    </span>
  );
}
