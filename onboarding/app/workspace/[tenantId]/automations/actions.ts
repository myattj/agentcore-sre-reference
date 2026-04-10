"use server";

import { revalidatePath } from "next/cache";

import { BridgeApiError, patchTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { BotPolicyConfig, EscalationRoute } from "@/lib/types";

export type SaveResult = { ok: true } | { ok: false; error: string };

export async function saveBotPolicy(
  tenantId: string,
  botPolicy: BotPolicyConfig,
): Promise<SaveResult> {
  const { token } = await requireSession(tenantId);
  try {
    await patchTenant(tenantId, token, { bot_policy: botPolicy });
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
  revalidatePath(`/workspace/${tenantId}/automations`);
  return { ok: true };
}

export async function saveEscalationRoutes(
  tenantId: string,
  routes: EscalationRoute[],
): Promise<SaveResult> {
  const { token } = await requireSession(tenantId);
  try {
    await patchTenant(tenantId, token, { escalation: { routes } });
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
  revalidatePath(`/workspace/${tenantId}/automations`);
  return { ok: true };
}
