/**
 * `/ops` — operator roster dashboard.
 *
 * Cross-tenant snapshot: who's using the platform, who's erroring,
 * who's spending. Sorted by invocation volume (most active first),
 * with "bad day" tenants (error rate > 5%) highlighted so an operator
 * can eyeball the platform in a few seconds.
 *
 * Auth: shared-secret cookie set by `/ops/login`. The ADMIN_SECRET env
 * var is also passed through to the bridge as the `X-Admin-Token`
 * header. Both sides must agree or the bridge returns 401.
 *
 * Fail-open rendering: if the bridge errors, we show a helpful
 * message instead of throwing. Metrics are diagnostic, not load-bearing.
 */
import Link from "next/link";

import { BridgeApiError, getOpsRoster, type MetricsWindow, type OpsRosterResponse } from "@/lib/bridge";
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

export default async function OpsRosterPage({
  searchParams,
}: {
  searchParams: Promise<{ window?: string; show_internal?: string }>;
}) {
  const sp = await searchParams;
  const window: MetricsWindow = isMetricsWindow(sp.window) ? sp.window : "7d";
  // Opt-in toggle for showing internal-testenv tenants in the roster.
  // Default off so the manual-test rig doesn't pollute real-customer
  // views. Any truthy value flips it on; we stringify for the URL.
  const showInternal = sp.show_internal === "1" || sp.show_internal === "true";

  let adminSecret: string;
  try {
    adminSecret = await requireOpsSession();
  } catch (e) {
    if (e instanceof Error && e.message === "ops-disabled") {
      return <OpsDisabled />;
    }
    throw e;
  }

  let roster: OpsRosterResponse;
  try {
    roster = await getOpsRoster(adminSecret, window, showInternal);
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Roster unavailable</h2>
          <p className="text-sm">Bridge returned an error: {e.detail}</p>
        </div>
      );
    }
    throw e;
  }

  const totalInvocations = roster.tenants.reduce((acc, t) => acc + t.invocations, 0);
  const totalErrors = roster.tenants.reduce((acc, t) => acc + t.errors, 0);
  const totalCost = roster.tenants.reduce((acc, t) => acc + t.cost_cents, 0);
  const platformErrorRate =
    totalInvocations > 0 ? (100 * totalErrors) / totalInvocations : 0;
  const badDayCount = roster.tenants.filter((t) => t.error_rate_pct > 5).length;

  return (
    <div>
      <header className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div>
          <h1 className="mb-2 text-2xl font-semibold tracking-tight">
            Platform roster
          </h1>
          <p className="text-sm text-[color:var(--muted)]">
            Every tenant with invocation metrics in the last {window}.
            Sorted by volume. Red rows are tenants with error rate above 5%.
            {showInternal ? (
              <>
                {" "}
                <span className="font-semibold text-amber-700">
                  Including internal testenv tenants.
                </span>
              </>
            ) : null}
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <div className="flex gap-1 rounded-md border border-[color:var(--border)] p-1">
            {WINDOWS.map((w) => (
              <Link
                key={w.value}
                href={`/ops?window=${w.value}${showInternal ? "&show_internal=1" : ""}`}
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
          <Link
            href={`/ops?window=${window}${showInternal ? "" : "&show_internal=1"}`}
            className="text-xs text-[color:var(--muted)] hover:text-[color:var(--accent)] hover:underline"
          >
            {showInternal
              ? "Hide internal testenv tenants"
              : "Show internal testenv tenants"}
          </Link>
        </div>
      </header>

      <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-4">
        <SummaryTile title="Tenants active" value={roster.tenants.length.toString()} />
        <SummaryTile title="Invocations" value={totalInvocations.toLocaleString()} />
        <SummaryTile
          title="Error rate"
          value={`${platformErrorRate.toFixed(1)}%`}
          tone={platformErrorRate > 5 ? "warn" : "ok"}
        />
        <SummaryTile
          title="Having a bad day"
          value={badDayCount.toString()}
          tone={badDayCount > 0 ? "warn" : "ok"}
        />
      </div>

      <div
        aria-label="Tenant metrics roster"
        className="mb-6 overflow-x-auto rounded-lg border border-[color:var(--border)] bg-white"
        role="region"
        tabIndex={0}
      >
        <table className="min-w-[680px] w-full text-sm">
          <thead>
            <tr className="border-b border-[color:var(--border)] text-left text-xs uppercase tracking-wider text-[color:var(--muted)]">
              <th className="px-4 py-3">Tenant</th>
              <th className="px-4 py-3 text-right">Invocations</th>
              <th className="px-4 py-3 text-right">Errors</th>
              <th className="px-4 py-3 text-right">Error rate</th>
              <th className="px-4 py-3 text-right">Cost</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {roster.tenants.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-8 text-center text-[color:var(--muted)]"
                >
                  No active tenants in this window.
                </td>
              </tr>
            ) : (
              roster.tenants.map((t) => (
                <tr
                  key={t.tenant_id}
                  className={
                    "border-b border-[color:var(--border)] last:border-b-0 " +
                    (t.error_rate_pct > 5 ? "bg-red-50" : "")
                  }
                >
                  <td className="px-4 py-3 font-mono text-xs">{t.tenant_id}</td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    {t.invocations.toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    {t.errors.toLocaleString()}
                  </td>
                  <td
                    className={
                      "px-4 py-3 text-right tabular-nums " +
                      (t.error_rate_pct > 5 ? "font-semibold text-red-700" : "")
                    }
                  >
                    {t.error_rate_pct.toFixed(1)}%
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    {formatCost(t.cost_cents)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      href={`/ops/${encodeURIComponent(t.tenant_id)}?window=${window}`}
                      className="text-xs text-[color:var(--accent)] hover:underline"
                    >
                      Drill in →
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-[color:var(--muted)]">
        Platform total cost over {window}:{" "}
        <span className="font-semibold">{formatCost(totalCost)}</span>
      </p>
    </div>
  );
}

function SummaryTile({
  title,
  value,
  tone = "ok",
}: {
  title: string;
  value: string;
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
    </div>
  );
}

function OpsDisabled() {
  return (
    <div className="mx-auto max-w-lg pt-16 text-center">
      <h2 className="mb-2 text-lg font-semibold">Ops dashboard disabled</h2>
      <p className="text-sm text-[color:var(--muted)]">
        This deployment has no <code className="font-mono">ADMIN_SECRET</code>{" "}
        configured, so cross-tenant metrics are unreachable. Set the env var
        on the onboarding service and redeploy to enable the operator view.
      </p>
    </div>
  );
}

function formatCost(cents: number): string {
  if (cents === 0) return "$0.00";
  if (cents < 100) return `${cents}¢`;
  return `$${(cents / 100).toFixed(2)}`;
}
