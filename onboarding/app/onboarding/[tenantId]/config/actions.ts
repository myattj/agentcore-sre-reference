/**
 * Server action for saving tenant config from the ConfigForm.
 *
 * Why a server action: it lets the client form submit without exposing
 * the bridge URL or session token to the browser. The cookie is read
 * server-side via `requireSession`, the bridge fetch happens server-side
 * in `lib/bridge.ts`, and on success we `revalidatePath` so the next
 * server-side render of `/onboarding/[tenantId]/config` shows fresh
 * values.
 *
 * The action validates auth with `requireSession`, which throws (via
 * `redirect`) on session failure — the form caller never sees a `null`.
 */
"use server";

import { revalidatePath } from "next/cache";

import { BridgeApiError, patchTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { TenantConfigPatch } from "@/lib/types";

export type SaveTenantResult =
  | { ok: true }
  | { ok: false; error: string };

export async function saveTenantConfig(
  tenantId: string,
  patch: TenantConfigPatch,
): Promise<SaveTenantResult> {
  const { token } = await requireSession(tenantId);
  try {
    await patchTenant(tenantId, token, patch);
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
  revalidatePath(`/onboarding/${tenantId}/config`);
  return { ok: true };
}
