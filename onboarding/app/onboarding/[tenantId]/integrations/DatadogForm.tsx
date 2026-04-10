"use client";

import { useState, useTransition } from "react";

import { connectDatadogAction, type ConnectResult } from "./actions";

export function DatadogForm({
  tenantId,
  initialConnected,
}: {
  tenantId: string;
  initialConnected: boolean;
}) {
  const [isPending, startTransition] = useTransition();
  const [result, setResult] = useState<ConnectResult | null>(null);
  const [connected, setConnected] = useState(initialConnected);

  function handleSubmit(formData: FormData) {
    const apiKey = (formData.get("apiKey") as string)?.trim();
    const appKey = (formData.get("appKey") as string)?.trim();
    const site = (formData.get("site") as string)?.trim() || "datadoghq.com";

    if (!apiKey || !appKey) {
      setResult({ ok: false, error: "Both API key and Application key are required" });
      return;
    }

    startTransition(async () => {
      const res = await connectDatadogAction(tenantId, apiKey, appKey, site);
      setResult(res);
      if (res.ok) setConnected(true);
    });
  }

  if (connected) {
    return (
      <div className="flex items-start justify-between rounded-lg border border-green-200 bg-green-50 p-5">
        <div className="min-w-0 flex-1">
          <h3 className="mb-1 font-semibold">Datadog</h3>
          <p className="text-xs text-[color:var(--muted)]">
            Pull metrics, logs, and recent alerts during triage.
          </p>
        </div>
        <span className="ml-4 shrink-0 rounded-full border border-green-300 bg-green-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-green-700">
          Connected
        </span>
      </div>
    );
  }

  return (
    <form
      action={handleSubmit}
      className="rounded-lg border border-[color:var(--border)] bg-white p-5"
    >
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="mb-1 font-semibold">Datadog</h3>
          <p className="text-xs text-[color:var(--muted)]">
            Pull metrics, logs, and recent alerts during triage.
          </p>
        </div>
      </div>

      <div className="mt-3 space-y-2">
        <label className="block text-xs font-medium">
          API Key
          <input
            name="apiKey"
            type="password"
            autoComplete="off"
            placeholder="dd-api-key..."
            className="mt-1 block w-full rounded border border-[color:var(--border)] px-2.5 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block text-xs font-medium">
          Application Key
          <input
            name="appKey"
            type="password"
            autoComplete="off"
            placeholder="dd-app-key..."
            className="mt-1 block w-full rounded border border-[color:var(--border)] px-2.5 py-1.5 text-sm"
            required
          />
        </label>
        <label className="block text-xs font-medium">
          Site
          <select
            name="site"
            className="mt-1 block w-full rounded border border-[color:var(--border)] px-2.5 py-1.5 text-sm"
          >
            <option value="datadoghq.com">datadoghq.com (US1)</option>
            <option value="us3.datadoghq.com">us3.datadoghq.com (US3)</option>
            <option value="us5.datadoghq.com">us5.datadoghq.com (US5)</option>
            <option value="datadoghq.eu">datadoghq.eu (EU)</option>
            <option value="ap1.datadoghq.com">ap1.datadoghq.com (AP1)</option>
          </select>
        </label>
      </div>

      {result && !result.ok && (
        <p className="mt-2 text-xs text-red-600">{result.error}</p>
      )}

      <button
        type="submit"
        disabled={isPending}
        className="mt-3 rounded-full bg-[color:var(--accent)] px-4 py-1.5 text-xs font-medium text-white hover:bg-[color:var(--accent-hover)] disabled:opacity-50"
      >
        {isPending ? "Connecting..." : "Connect"}
      </button>
    </form>
  );
}
