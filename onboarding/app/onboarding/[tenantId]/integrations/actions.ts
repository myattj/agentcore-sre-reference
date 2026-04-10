"use server";

import { revalidatePath } from "next/cache";

import {
  BridgeApiError,
  connectConfluence,
  connectDatadog,
  connectGitHub,
  connectJira,
  connectLinear,
  connectNotion,
  connectPagerDuty,
} from "@/lib/bridge";
import { requireSession } from "@/lib/session";

export type ConnectResult =
  | { ok: true; target_name: string }
  | { ok: false; error: string };

// ---------------------------------------------------------------------------
// Datadog
// ---------------------------------------------------------------------------

export async function connectDatadogAction(
  tenantId: string,
  apiKey: string,
  appKey: string,
  site: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectDatadog(tenantId, token, {
      api_key: apiKey,
      app_key: appKey,
      site: site || "datadoghq.com",
    });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// Confluence
// ---------------------------------------------------------------------------

export async function connectConfluenceAction(
  tenantId: string,
  email: string,
  apiToken: string,
  domain: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectConfluence(tenantId, token, {
      email,
      api_token: apiToken,
      domain,
    });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// Notion
// ---------------------------------------------------------------------------

export async function connectNotionAction(
  tenantId: string,
  integrationToken: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectNotion(tenantId, token, {
      integration_token: integrationToken,
    });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// Jira
// ---------------------------------------------------------------------------

export async function connectJiraAction(
  tenantId: string,
  email: string,
  apiToken: string,
  domain: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectJira(tenantId, token, {
      email,
      api_token: apiToken,
      domain,
    });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// Linear
// ---------------------------------------------------------------------------

export async function connectLinearAction(
  tenantId: string,
  apiKey: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectLinear(tenantId, token, { api_key: apiKey });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// PagerDuty
// ---------------------------------------------------------------------------

export async function connectPagerDutyAction(
  tenantId: string,
  apiKey: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectPagerDuty(tenantId, token, { api_key: apiKey });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}

// ---------------------------------------------------------------------------
// GitHub
// ---------------------------------------------------------------------------

export async function connectGitHubAction(
  tenantId: string,
  personalAccessToken: string,
  org: string,
): Promise<ConnectResult> {
  const { token } = await requireSession(tenantId);
  try {
    const resp = await connectGitHub(tenantId, token, {
      personal_access_token: personalAccessToken,
      org: org || undefined,
    });
    if (!resp.ok) {
      return { ok: false, error: resp.error ?? "unknown error" };
    }
    revalidatePath(`/onboarding/${tenantId}/integrations`);
    return { ok: true, target_name: resp.target_name ?? "" };
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
}
