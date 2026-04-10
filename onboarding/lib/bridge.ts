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
    let detail = response.statusText;
    try {
      const data = (await response.json()) as { detail?: string };
      if (data?.detail) detail = data.detail;
    } catch {
      // Body wasn't JSON; keep the status text.
    }
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

export function connectDatadog(
  tenantId: string,
  token: string,
  body: { api_key: string; app_key: string; site?: string },
): Promise<IntegrationConnectResponse> {
  return bridgeFetch<IntegrationConnectResponse>(
    `/api/tenants/${encodeURIComponent(tenantId)}/integrations/datadog`,
    { token, method: "POST", body },
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
