/**
 * Server action for saving per-channel persona overrides.
 *
 * Called by `ChannelTabs` when the user edits a channel's persona
 * (system_prompt, allowed_tools, memory_rules). The action patches the
 * tenant's `channels` dict via `PATCH /api/tenants/{id}`.
 */
"use server";

import { revalidatePath } from "next/cache";

import { BridgeApiError, patchTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { ChannelPersona } from "@/lib/types";

export type SaveChannelResult =
  | { ok: true }
  | { ok: false; error: string };

export async function saveChannelPersona(
  tenantId: string,
  channelId: string,
  persona: ChannelPersona,
): Promise<SaveChannelResult> {
  const { token } = await requireSession(tenantId);
  try {
    await patchTenant(tenantId, token, {
      channels: { [channelId]: persona },
    });
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
  revalidatePath(`/workspace/${tenantId}/channels`);
  return { ok: true };
}
