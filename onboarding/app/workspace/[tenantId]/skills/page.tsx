import { BridgeApiError, getTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";

import SkillsEditor from "./SkillsEditor";

export default async function SkillsPage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  const { token } = await requireSession(tenantId);

  let tenant;
  try {
    tenant = await getTenant(tenantId, token);
  } catch (e) {
    if (e instanceof BridgeApiError && e.status === 404) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-900">
          <h2 className="mb-2 font-semibold">Tenant not found</h2>
          <p className="text-sm">
            We couldn&apos;t find your tenant in our database.
          </p>
        </div>
      );
    }
    throw e;
  }

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">
          Skills &amp; Runbooks
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Your bot already knows how to triage alerts, answer questions,
          summarize threads, and do on-call handoffs — that&apos;s baked
          into the default prompt, no skills required. Use this page
          only for <em>custom</em> runbooks with explicit slash-command
          triggers (e.g. <code className="rounded bg-[color:var(--card)] px-1 font-mono text-[11px]">/deploy-status</code>).
        </p>
      </header>

      <SkillsEditor tenantId={tenantId} initial={tenant} />
    </div>
  );
}
