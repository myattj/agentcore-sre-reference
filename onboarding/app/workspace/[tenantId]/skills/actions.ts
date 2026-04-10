"use server";

import { revalidatePath } from "next/cache";

import { BridgeApiError, patchTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";
import type { SkillDef } from "@/lib/types";

export type SaveSkillsResult = { ok: true } | { ok: false; error: string };

export async function saveSkills(
  tenantId: string,
  skills: SkillDef[],
): Promise<SaveSkillsResult> {
  const { token } = await requireSession(tenantId);
  try {
    await patchTenant(tenantId, token, { skills });
  } catch (e) {
    if (e instanceof BridgeApiError) {
      return { ok: false, error: e.detail };
    }
    return { ok: false, error: "unexpected error" };
  }
  revalidatePath(`/workspace/${tenantId}/skills`);
  return { ok: true };
}
