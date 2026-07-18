/**
 * `/ops/[tenantId]` — operator drill-down into one tenant's metrics.
 *
 * Same metric shape as `/workspace/[tenantId]/metrics` but the auth path
 * is different: this page uses the shared-secret ops cookie and hits
 * the bridge's `/api/ops/metrics/tenants/{id}` route. An operator can
 * inspect any tenant without holding a session token for it.
 *
 * Chart widgets are intentionally identical to the tenant-facing page
 * so nothing is surprising when we eventually merge them behind a real
 * identity model.
 */
import Link from "next/link";

import {
  BridgeApiError,
  getOpsTenantMetrics,
  type MetricsWindow,
  type TenantMetricsSnapshot,
} from "@/lib/bridge";
import { requireOpsSession } from "@/lib/ops";

const WINDOWS: { value: MetricsWindow; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

function isMetricsWindow(v: string | undefined): v is MetricsWindow {
  return v === "1h" || v === "24h" || v === "7d" || v === "30d";
}

export default async function OpsTenantDrilldownPage({
  params,
  searchParams,
}: {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ window?: string }>;
}) {
  const { tenantId } = await params;
  const sp = await searchParams;
  const window: MetricsWindow = isMetricsWindow(sp.window) ? sp.window : "7d";

  let adminSecret: string;
  try {
    adminSecret = await requireOpsSession();
  } catch (e) {
    if (e instanceof Error && e.message === "ops-disabled") {
      return (
        <div className="mx-auto max-w-lg pt-16 text-center text-sm text-[color:var(--muted)]">
          Ops dashboard is disabled on this deploy.
        </div>
      );
    }
    throw e;
  }

  let snapshot: TenantMetricsSnapshot;
  try {
    snapshot = await getOpsTenantMetrics(tenantId, adminSecret, window);
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Metrics unavailable</h2>
          <p className="text-sm">Bridge returned: {e.detail}</p>
        </div>
      );
    }
    throw e;
  }

  return (
    <div>
      <header className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div>
          <Link
            href={`/ops?window=${window}`}
            className="mb-2 inline-block text-xs text-[color:var(--muted)] hover:text-[color:var(--fg)]"
          >
            ← Roster
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">
            <span className="font-mono text-lg">{tenantId}</span>
          </h1>
          <p className="text-sm text-[color:var(--muted)]">
            Operator drill-down. Data from the CloudWatch Agent/Runtime namespace.
          </p>
        </div>
        <div className="flex gap-1 rounded-md border border-[color:var(--border)] p-1">
          {WINDOWS.map((w) => (
            <Link
              key={w.value}
              href={`/ops/${encodeURIComponent(tenantId)}?window=${w.value}`}
              className={
                w.value === window
                  ? "rounded bg-[color:var(--accent)] px-3 py-1 text-xs font-semibold text-white"
                  : "rounded px-3 py-1 text-xs text-[color:var(--muted)] hover:bg-[color:var(--card)]"
              }
            >
              {w.label}
            </Link>
          ))}
        </div>
      </header>

      {snapshot.error ? (
        <div className="mb-6 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          Partial read: {snapshot.error}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Tile
          title="Invocations"
          value={snapshot.invocations_total.toLocaleString()}
          subtitle={`over ${snapshot.window}`}
        />
        <Tile
          title="Error rate"
          value={`${snapshot.error_rate_pct.toFixed(1)}%`}
          subtitle={`${snapshot.errors_total.toLocaleString()} errors`}
          tone={snapshot.error_rate_pct > 5 ? "warn" : "ok"}
        />
        <Tile
          title="Estimated cost"
          value={formatCost(snapshot.estimated_cost_cents_total)}
          subtitle={`${(snapshot.input_tokens_total + snapshot.output_tokens_total).toLocaleString()} tokens`}
        />
        <Tile
          title="Latency"
          value={`${Math.round(snapshot.p50_duration_ms)}ms`}
          subtitle={`p95 ${Math.round(snapshot.p95_duration_ms)}ms`}
        />
      </div>

      <div className="mt-6 rounded-lg border border-[color:var(--border)] bg-white p-5">
        <p className="mb-3 text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
          Top tools
        </p>
        {snapshot.top_tools.length === 0 ? (
          <p className="text-sm text-[color:var(--muted)]">
            No tool calls in this window.
          </p>
        ) : (
          <ul className="space-y-2">
            {snapshot.top_tools.map((t) => (
              <li key={t.tool_name} className="flex justify-between text-sm">
                <span className="font-mono">{t.tool_name}</span>
                <span className="tabular-nums">
                  {t.calls}
                  {t.errors > 0 ? (
                    <span className="ml-2 text-amber-700">
                      ({t.errors} err)
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Tile({
  title,
  value,
  subtitle,
  tone = "ok",
}: {
  title: string;
  value: string;
  subtitle: string;
  tone?: "ok" | "warn";
}) {
  return (
    <div className="rounded-lg border border-[color:var(--border)] bg-white p-5">
      <p className="text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
        {title}
      </p>
      <p
        className={`mt-2 text-2xl font-semibold tabular-nums ${
          tone === "warn" ? "text-amber-700" : ""
        }`}
      >
        {value}
      </p>
      <p className="mt-1 text-xs text-[color:var(--muted)]">{subtitle}</p>
    </div>
  );
}

function formatCost(cents: number): string {
  if (cents === 0) return "$0.00";
  if (cents < 100) return `${cents}¢`;
  return `$${(cents / 100).toFixed(2)}`;
}
