/**
 * Server-side fetch wrapper for the bridge API.
 *
 * Every call goes from the Next.js server to the bridge — never from the
 * browser. This sidesteps CORS entirely and keeps the session token off
 * the client. See CLAUDE.md gotcha #24.
 *
 * `cache: "no-store"` is critical: Next.js aggressively caches `fetch()`
 * in server components, and we'd see stale config after a PATCH without
 * it. See CLAUDE.md gotcha #25.
 */
import { getBridgeUrl } from "./env";
import type {
  ChannelsResponse,
  GitHubAppInstallResponse,
  IntegrationConnectResponse,
  TenantConfig,
  TenantConfigPatch,
} from "./types";

export class BridgeApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`Bridge API error ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

type BridgeFetchOptions = {
  token: string;
  method?: "GET" | "PATCH" | "POST" | "DELETE";
  body?: unknown;
};

function normalizedErrorDetail(detail: unknown): string | null {
  if (typeof detail === "string") {
    const message = detail.trim();
    return message.length > 0 ? message : null;
  }
  if (!Array.isArray(detail)) return null;

  const messages: string[] = [];
  for (const item of detail) {
    if (typeof item !== "object" || item === null || !("msg" in item)) continue;
    const message = (item as { msg?: unknown }).msg;
    if (typeof message === "string" && message.trim().length > 0) {
      messages.push(message.trim());
    }
  }
  return messages.length > 0 ? messages.join("; ") : null;
}

async function responseErrorDetail(response: Response): Promise<string> {
  const fallback = response.statusText || "request failed";
  try {
    const payload = (await response.json()) as unknown;
    if (typeof payload !== "object" || payload === null || !("detail" in payload)) {
      return fallback;
    }
    return normalizedErrorDetail((payload as { detail?: unknown }).detail) ?? fallback;
  } catch {
    return fallback;
  }
}

async function bridgeFetch<T>(path: string, opts: BridgeFetchOptions): Promise<T> {
  const url = `${getBridgeUrl()}${path}`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${opts.token}`,
    Accept: "application/json",
  };
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await responseErrorDetail(response);
    throw new BridgeApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function getTenant(tenantId: string, token: string): Promise<TenantConfig> {
  return bridgeFetch<TenantConfig>(
    `/api/tenants/${encodeURIComponent(tenantId)}`,
    { token, method: "GET" },
  );
}

export function patchTenant(
  tenantId: string,
  token: string,
  patch: TenantConfigPatch,
): Promise<TenantConfig> {
  return bridgeFetch<TenantConfig>(
    `/api/tenants/${encodeURIComponent(tenantId)}`,
    { token, method: "PATCH", body: patch },
  );
}

export function listChannels(tenantId: string, token: string): Promise<ChannelsResponse> {
  return bridgeFetch<ChannelsResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/channels`,
    { token, method: "GET" },
  );
}

export function connectConfluence(
  tenantId: string,
  token: string,
  body: { email: string; api_token: string; domain: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/confluence`,
    { token, method: "POST", body },
  );
}

export function connectNotion(
  tenantId: string,
  token: string,
  body: { integration_token: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/notion`,
    { token, method: "POST", body },
  );
}

export function connectJira(
  tenantId: string,
  token: string,
  body: { email: string; api_token: string; domain: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/jira`,
    { token, method: "POST", body },
  );
}

export function connectLinear(
  tenantId: string,
  token: string,
  body: { api_key: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/linear`,
    { token, method: "POST", body },
  );
}

export function connectPagerDuty(
  tenantId: string,
  token: string,
  body: { api_key: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/pagerduty`,
    { token, method: "POST", body },
  );
}

export function connectGitHub(
  tenantId: string,
  token: string,
  body: { personal_access_token: string; org?: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/github`,
    { token, method: "POST", body },
  );
}

/**
 * Trigger the install-time warm-start for a GitHub App installation.
 *
 * Called from the /github/installed route handler after the user
 * completes the App install on github.com and is redirected back with
 * an `installation_id`. The bridge mints an installation token, lists
 * repos, ranks them, and writes a `codebases` block to the tenant row after
 * an operator has verified and bound the installation identity.
 *
 * Returns a full response (never throws on warm-start failures — the
 * bridge wraps GitHub/network errors into `{ok: false, error: ...}`).
 * BridgeApiError still fires for 4xx/5xx at the HTTP layer (auth, etc.).
 */
export function installGitHubApp(
  tenantId: string,
  token: string,
  installationId: string,
): Promise<GitHubAppInstallResponse> {
  return bridgeFetch<GitHubAppInstallResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/codebases/github/install`,
    { token, method: "POST", body: { installation_id: installationId } },
  );
}

// ---------------------------------------------------------------------------
// Metrics — CloudWatch data surfaced via bridge metrics_reader.py
// ---------------------------------------------------------------------------

/** One (timestamp, value) sample returned by CloudWatch GetMetricData. */
export type MetricSample = { t: string; v: number };

/** Top tool entry in a TenantMetricsSnapshot. */
export type TopTool = {
  tool_name: string;
  calls: number;
  errors: number;
};

/** Shape returned by GET /api/tenants/{id}/metrics and /api/ops/metrics/tenants/{id}. */
export type TenantMetricsSnapshot = {
  tenant_id: string;
  window: string;
  invocations_total: number;
  errors_total: number;
  error_rate_pct: number;
  input_tokens_total: number;
  output_tokens_total: number;
  estimated_cost_cents_total: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
  top_tools: TopTool[];
  invocations_timeseries: MetricSample[];
  errors_timeseries: MetricSample[];
  cost_timeseries: MetricSample[];
  error: string | null;
};

export type OpsRosterRow = {
  tenant_id: string;
  invocations: number;
  errors: number;
  error_rate_pct: number;
  cost_cents: number;
};

export type OpsRosterResponse = {
  window: string;
  tenants: OpsRosterRow[];
  include_testenv: boolean;
};

/** Accepted values for the `window` query parameter. */
export type MetricsWindow = "1h" | "24h" | "7d" | "30d";

export function getTenantMetrics(
  tenantId: string,
  token: string,
  window: MetricsWindow = "7d",
): Promise<TenantMetricsSnapshot> {
  return bridgeFetch<TenantMetricsSnapshot>(
    `/api/tenants/${encodeURIComponent(tenantId)}/metrics?window=${window}`,
    { token, method: "GET" },
  );
}

/**
 * Fetch from the bridge using the admin shared-secret header instead of
 * a session token. Used for the temporary ``/ops`` operator dashboard
 * until a real identity model lands.
 *
 * The secret is read from ``ADMIN_SECRET`` on the onboarding server (not
 * NEXT_PUBLIC_* — it must never reach the browser). The route handler
 * that calls this function is responsible for its own access control
 * (e.g. cookie set by a prior login page).
 */
async function adminFetch<T>(path: string, adminSecret: string): Promise<T> {
  const url = `${getBridgeUrl()}${path}`;
  const response = await fetch(url, {
    method: "GET",
    headers: {
      "X-Admin-Token": adminSecret,
      Accept: "application/json",
    },
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await responseErrorDetail(response);
    throw new BridgeApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function getOpsRoster(
  adminSecret: string,
  window: MetricsWindow = "7d",
  includeTestenv: boolean = false,
): Promise<OpsRosterResponse> {
  const q = new URLSearchParams({ window });
  if (includeTestenv) q.set("include_testenv", "true");
  return adminFetch<OpsRosterResponse>(
    `/api/ops/metrics/roster?${q.toString()}`,
    adminSecret,
  );
}

export function getOpsTenantMetrics(
  tenantId: string,
  adminSecret: string,
  window: MetricsWindow = "7d",
): Promise<TenantMetricsSnapshot> {
  return adminFetch<TenantMetricsSnapshot>(
    `/api/ops/metrics/tenants/${encodeURIComponent(tenantId)}?window=${window}`,
    adminSecret,
  );
}
