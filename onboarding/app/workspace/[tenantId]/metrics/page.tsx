/**
 * Tenant metrics page — session-scoped CloudWatch data.
 *
 * Renders an at-a-glance view of the tenant's agent traffic over a
 * configurable window (1h / 24h / 7d / 30d). Data comes from the bridge
 * `/api/tenants/{id}/metrics` route which queries CloudWatch using the
 * `AgentCore Reference/Agent` namespace populated via EMF by the agent.
 *
 * This is a pure server component — no client-side state, no charts
 * library. A lightweight inline SVG sparkline gives the shape of each
 * timeseries without adding a dependency. For richer drill-downs an
 * operator can click through to the CloudWatch dashboard linked at the
 * bottom of the page.
 *
 * Auth: `requireSession(tenantId)` as with every other page in this
 * directory; `searchParams` is a Promise in Next.js 16 (gotcha #16).
 */
import Link from "next/link";

import { BridgeApiError, getTenantMetrics, type MetricsWindow, type TenantMetricsSnapshot } from "@/lib/bridge";
import { requireSession } from "@/lib/session";

const WINDOWS: { value: MetricsWindow; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

function isMetricsWindow(v: string | undefined): v is MetricsWindow {
  return v === "1h" || v === "24h" || v === "7d" || v === "30d";
}

export default async function TenantMetricsPage({
  params,
  searchParams,
}: {
  params: Promise<{ tenantId: string }>;
  searchParams: Promise<{ window?: string }>;
}) {
  const { tenantId } = await params;
  const sp = await searchParams;
  const window: MetricsWindow = isMetricsWindow(sp.window) ? sp.window : "7d";

  const { token } = await requireSession(tenantId);

  let snapshot: TenantMetricsSnapshot;
  try {
    snapshot = await getTenantMetrics(tenantId, token, window);
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Metrics unavailable</h2>
          <p className="text-sm">
            Couldn&apos;t reach the metrics service: {e.detail}
          </p>
        </div>
      );
    }
    throw e;
  }

  const base = `/workspace/${encodeURIComponent(tenantId)}/metrics`;

  return (
    <div>
      <header className="mb-8 flex items-start justify-between gap-6">
        <div>
          <h1 className="mb-2 text-2xl font-semibold tracking-tight">Metrics</h1>
          <p className="text-sm text-[color:var(--muted)]">
            How your bot is actually being used. Pulled from CloudWatch;
            expect a few minutes of lag behind live traffic.
          </p>
        </div>
        <div className="flex gap-1 rounded-md border border-[color:var(--border)] p-1">
          {WINDOWS.map((w) => (
            <Link
              key={w.value}
              href={`${base}?window=${w.value}`}
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
          Metrics read returned an error: {snapshot.error}. Some tiles may
          show zero.
        </div>
      ) : null}

      {snapshot.invocations_total === 0 && !snapshot.error ? (
        <EmptyState tenantId={tenantId} />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
            <Tile
              title="Invocations"
              value={snapshot.invocations_total.toLocaleString()}
              subtitle={`over last ${snapshot.window}`}
              sparkline={snapshot.invocations_timeseries}
            />
            <Tile
              title="Error rate"
              value={`${snapshot.error_rate_pct.toFixed(1)}%`}
              subtitle={`${snapshot.errors_total.toLocaleString()} errors`}
              sparkline={snapshot.errors_timeseries}
              tone={snapshot.error_rate_pct > 5 ? "warn" : "ok"}
            />
            <Tile
              title="Estimated cost"
              value={formatCost(snapshot.estimated_cost_cents_total)}
              subtitle={`${(snapshot.input_tokens_total + snapshot.output_tokens_total).toLocaleString()} tokens`}
              sparkline={snapshot.cost_timeseries}
            />
            <Tile
              title="Latency"
              value={`${Math.round(snapshot.p50_duration_ms)}ms`}
              subtitle={`p95 ${Math.round(snapshot.p95_duration_ms)}ms`}
            />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <TokenBreakdown
              input={snapshot.input_tokens_total}
              output={snapshot.output_tokens_total}
            />
            <TopTools tools={snapshot.top_tools} />
          </div>
        </>
      )}

      <div className="mt-8 rounded-md border border-[color:var(--border)] bg-[color:var(--card)] p-4 text-xs text-[color:var(--muted)]">
        <p>
          For deeper drill-downs (per-tool latency, per-invocation traces,
          log queries) use the CloudWatch dashboard directly. Ask your
          account admin for the URL.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function EmptyState({ tenantId }: { tenantId: string }) {
  return (
    <div className="rounded-lg border border-dashed border-[color:var(--border)] bg-[color:var(--card)] p-8 text-center">
      <h2 className="mb-2 text-sm font-semibold">No activity yet</h2>
      <p className="text-sm text-[color:var(--muted)]">
        Once people start messaging your bot in Slack, you&apos;ll see
        invocations, error rates, cost, and top tools here. Tenant{" "}
        <code className="font-mono text-xs">{tenantId}</code> has no
        recorded invocations in this window.
      </p>
    </div>
  );
}

function Tile({
  title,
  value,
  subtitle,
  sparkline,
  tone = "ok",
}: {
  title: string;
  value: string;
  subtitle: string;
  sparkline?: { t: string; v: number }[];
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
      {sparkline && sparkline.length > 1 ? (
        <div className="mt-3">
          <Sparkline samples={sparkline} />
        </div>
      ) : null}
    </div>
  );
}

function Sparkline({ samples }: { samples: { t: string; v: number }[] }) {
  // Inline SVG sparkline. Avoids a chart library dep and renders in the
  // server component. ~60px tall, stretches to the tile width.
  const w = 200;
  const h = 40;
  const values = samples.map((s) => s.v);
  const max = Math.max(...values, 1);
  const step = samples.length > 1 ? w / (samples.length - 1) : 0;
  const points = samples
    .map((s, i) => {
      const x = i * step;
      const y = h - (s.v / max) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="h-10 w-full"
      preserveAspectRatio="none"
    >
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        className="text-[color:var(--accent)]"
      />
    </svg>
  );
}

function TokenBreakdown({ input, output }: { input: number; output: number }) {
  const total = input + output;
  const inputPct = total > 0 ? (input / total) * 100 : 0;
  return (
    <div className="rounded-lg border border-[color:var(--border)] bg-white p-5">
      <p className="text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
        Token breakdown
      </p>
      <div className="mt-3 space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-[color:var(--muted)]">Input</span>
          <span className="font-mono tabular-nums">{input.toLocaleString()}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[color:var(--muted)]">Output</span>
          <span className="font-mono tabular-nums">{output.toLocaleString()}</span>
        </div>
      </div>
      {total > 0 ? (
        <div className="mt-3 h-2 overflow-hidden rounded bg-[color:var(--card)]">
          <div
            className="h-full bg-[color:var(--accent)]"
            style={{ width: `${inputPct}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}

function TopTools({
  tools,
}: {
  tools: { tool_name: string; calls: number; errors: number }[];
}) {
  if (tools.length === 0) {
    return (
      <div className="rounded-lg border border-[color:var(--border)] bg-white p-5">
        <p className="text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
          Top tools
        </p>
        <p className="mt-3 text-sm text-[color:var(--muted)]">
          No tool calls in this window.
        </p>
      </div>
    );
  }
  const maxCalls = Math.max(...tools.map((t) => t.calls), 1);
  return (
    <div className="rounded-lg border border-[color:var(--border)] bg-white p-5">
      <p className="mb-3 text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
        Top tools
      </p>
      <ul className="space-y-2">
        {tools.map((t) => (
          <li key={t.tool_name}>
            <div className="mb-1 flex justify-between text-sm">
              <span className="font-mono">{t.tool_name}</span>
              <span className="tabular-nums">
                {t.calls}
                {t.errors > 0 ? (
                  <span className="ml-1 text-amber-700">
                    ({t.errors} err)
                  </span>
                ) : null}
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded bg-[color:var(--card)]">
              <div
                className="h-full bg-[color:var(--accent)]"
                style={{ width: `${(t.calls / maxCalls) * 100}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function formatCost(cents: number): string {
  if (cents === 0) return "$0.00";
  if (cents < 100) return `${cents}¢`;
  return `$${(cents / 100).toFixed(2)}`;
}
