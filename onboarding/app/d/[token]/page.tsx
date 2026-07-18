/**
 * Ephemeral dashboard page.
 *
 * Renders a bot-generated dashboard from a spec stored in DynamoDB.
 * The URL token is unguessable (UUID) so no session auth is required —
 * anyone with the link can view.
 *
 * Data flow: Next.js server component -> bridge /internal/dashboard
 * with the bearer token in a non-logged request header
 * -> DynamoDB -> JSON spec -> React client component renders panels.
 */
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { cache } from "react";
import { getBridgeUrl } from "@/lib/env";
import { DashboardRenderer } from "./DashboardRenderer";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export type DashboardSpec = {
  created_at: string;
  ttl: number;
  title: string;
  panels: Panel[];
};

export type Panel =
  | ChartPanel
  | TablePanel
  | StatPanel
  | TextPanel
  | ListPanel;

export type ChartPanel = {
  type: "chart";
  title?: string;
  chart_type: "line" | "bar" | "pie" | "area" | "scatter";
  labels: string[];
  datasets: { label: string; data: number[] }[];
  x_label?: string;
  y_label?: string;
};

export type TablePanel = {
  type: "table";
  title?: string;
  columns: string[];
  rows: (string | number | boolean | null)[][];
};

export type StatPanel = {
  type: "stat";
  title?: string;
  value: string;
  delta?: string;
  trend?: "up" | "down" | "flat";
};

export type TextPanel = {
  type: "text";
  title?: string;
  content: string;
};

export type ListPanel = {
  type: "list";
  title?: string;
  items: { key: string; value: string }[];
};

const fetchDashboard = cache(async (token: string): Promise<DashboardSpec | null> => {
  const url = `${getBridgeUrl()}/internal/dashboard`;
  const res = await fetch(url, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "X-Dashboard-Token": token,
    },
    signal: AbortSignal.timeout(5_000),
  });
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new Error(`Dashboard service returned ${res.status}`);
  }
  return (await res.json()) as DashboardSpec;
});

function formatUtc(value: string | number): string {
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.valueOf())) return "unknown";
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(date) + " UTC";
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ token: string }>;
}): Promise<Metadata> {
  const { token } = await params;
  const spec = await fetchDashboard(token);
  return {
    title: spec?.title ?? "Dashboard",
    robots: { index: false, follow: false, nocache: true },
  };
}

export default async function DashboardPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const spec = await fetchDashboard(token);
  if (!spec) notFound();

  return (
    <main className="min-h-screen bg-[color:var(--background)] px-4 py-8 sm:px-8">
      <div className="mx-auto max-w-6xl">
        <header className="mb-8">
          <h1 className="text-2xl font-semibold text-[color:var(--foreground)] sm:text-3xl">
            {spec.title}
          </h1>
          <p className="mt-1 text-sm text-[color:var(--muted)]">
            Created {formatUtc(spec.created_at)} &middot; Expires {formatUtc(spec.ttl)}
          </p>
        </header>
        <DashboardRenderer panels={spec.panels} />
      </div>
    </main>
  );
}
