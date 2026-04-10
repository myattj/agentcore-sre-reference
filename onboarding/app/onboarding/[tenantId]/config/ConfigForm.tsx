/**
 * Client form for editing the tenant's system prompt and catalog tools.
 *
 * Controlled inputs + a server action submission. Uses
 * `useTransition` so the save button can show a pending state without
 * blocking the rest of the page.
 */
"use client";

import { useState, useTransition } from "react";

import { KNOWN_CATALOG_TOOLS, type TenantConfig } from "@/lib/types";

import { type SaveTenantResult, saveTenantConfig } from "./actions";

type Status =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

type Props = {
  tenantId: string;
  initial: TenantConfig;
};

export default function ConfigForm({ tenantId, initial }: Props) {
  const [systemPrompt, setSystemPrompt] = useState(initial.system_prompt);
  const [allowedTools, setAllowedTools] = useState<string[]>(
    initial.catalog.allowed_tools,
  );
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [isPending, startTransition] = useTransition();

  const dirty =
    systemPrompt !== initial.system_prompt ||
    allowedTools.length !== initial.catalog.allowed_tools.length ||
    allowedTools.some(
      (t, i) => t !== initial.catalog.allowed_tools[i],
    );

  function toggleTool(toolId: string) {
    setAllowedTools((current) =>
      current.includes(toolId)
        ? current.filter((t) => t !== toolId)
        : [...current, toolId],
    );
  }

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!dirty || isPending) return;
    setStatus({ kind: "pending" });
    startTransition(async () => {
      const result: SaveTenantResult = await saveTenantConfig(tenantId, {
        system_prompt: systemPrompt,
        catalog: { allowed_tools: allowedTools },
      });
      if (result.ok) {
        setStatus({ kind: "saved" });
      } else {
        setStatus({ kind: "error", message: result.error });
      }
    });
  }

  return (
    <form onSubmit={onSubmit} className="space-y-8">
      <div>
        <label
          htmlFor="system_prompt"
          className="mb-2 block text-sm font-medium"
        >
          System prompt
        </label>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          The personality and instructions the agent uses for every reply.
          Be specific about who the agent is, what kinds of questions it
          should answer, and how it should behave when it doesn&apos;t know.
        </p>
        <textarea
          id="system_prompt"
          name="system_prompt"
          required
          minLength={1}
          rows={6}
          value={systemPrompt}
          onChange={(e) => {
            setSystemPrompt(e.target.value);
            setStatus({ kind: "idle" });
          }}
          className="w-full rounded-lg border border-[color:var(--border)] bg-white p-3 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
        />
      </div>

      <div>
        <h3 className="mb-2 text-sm font-medium">Catalog tools</h3>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          Tools the agent can call. Tools you don&apos;t enable here are
          invisible to the agent.
        </p>
        <ul className="space-y-2">
          {KNOWN_CATALOG_TOOLS.map((tool) => (
            <li key={tool.id}>
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-3 transition hover:border-[color:var(--accent)]/40">
                <input
                  type="checkbox"
                  checked={allowedTools.includes(tool.id)}
                  onChange={() => {
                    toggleTool(tool.id);
                    setStatus({ kind: "idle" });
                  }}
                  className="mt-1 h-4 w-4 cursor-pointer rounded border-[color:var(--border)] text-[color:var(--accent)] focus:ring-[color:var(--accent)]"
                />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{tool.label}</div>
                  <div className="text-xs text-[color:var(--muted)]">
                    {tool.description}
                  </div>
                </div>
              </label>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex items-center gap-4">
        <button
          type="submit"
          disabled={!dirty || isPending}
          className="rounded-full bg-[color:var(--accent)] px-6 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-[color:var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isPending ? "Saving…" : "Save changes"}
        </button>
        {status.kind === "saved" ? (
          <span className="text-sm text-green-600">Saved.</span>
        ) : null}
        {status.kind === "error" ? (
          <span className="text-sm text-red-600">
            Couldn&apos;t save: {status.message}
          </span>
        ) : null}
      </div>
    </form>
  );
}
