"use client";

import { useState, useTransition } from "react";

import { connectPagerDutyAction, type ConnectResult } from "./actions";

export function PagerDutyForm({ tenantId, initialConnected }: { tenantId: string; initialConnected: boolean }) {
  const [isPending, startTransition] = useTransition();
  const [result, setResult] = useState<ConnectResult | null>(null);
  const [connected, setConnected] = useState(initialConnected);

  function handleSubmit(formData: FormData) {
    const apiKey = (formData.get("apiKey") as string)?.trim();
    if (!apiKey) {
      setResult({ ok: false, error: "API key is required" });
      return;
    }
    startTransition(async () => {
      const res = await connectPagerDutyAction(tenantId, apiKey);
      setResult(res);
      if (res.ok) setConnected(true);
    });
  }

  if (connected) {
    return (
      <div className="flex items-start justify-between rounded-lg border border-green-200 bg-green-50 p-5">
        <div className="min-w-0 flex-1">
          <h3 className="mb-1 font-semibold">PagerDuty</h3>
          <p className="text-xs text-[color:var(--muted)]">Look up incidents, the on-call schedule, and page on-call.</p>
        </div>
        <span className="ml-4 shrink-0 rounded-full border border-green-300 bg-green-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-green-700">Connected</span>
      </div>
    );
  }

  return (
    <form action={handleSubmit} className="rounded-lg border border-[color:var(--border)] bg-white p-5">
      <h3 className="mb-1 font-semibold">PagerDuty</h3>
      <p className="mb-3 text-xs text-[color:var(--muted)]">Look up incidents, the on-call schedule, and page on-call.</p>
      <div className="space-y-2">
        <label className="block text-xs font-medium">REST API Key (v2)<input name="apiKey" type="password" autoComplete="off" placeholder="PagerDuty API key" className="mt-1 block w-full rounded border border-[color:var(--border)] px-2.5 py-1.5 text-sm" required /></label>
      </div>
      {result && !result.ok && <p className="mt-2 text-xs text-red-600">{result.error}</p>}
      <button type="submit" disabled={isPending} className="mt-3 rounded-full bg-[color:var(--accent)] px-4 py-1.5 text-xs font-medium text-white hover:bg-[color:var(--accent-hover)] disabled:opacity-50">{isPending ? "Connecting..." : "Connect"}</button>
    </form>
  );
}
