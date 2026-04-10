/**
 * Config page — first step of the onboarding flow.
 *
 * Server component that:
 *   1. Verifies the session cookie via `requireSession`
 *   2. Fetches the current tenant config from the bridge API
 *   3. Renders the client form, prefilled with the current values
 */
import Link from "next/link";

import { BridgeApiError, getTenant } from "@/lib/bridge";
import { requireSession } from "@/lib/session";

import ConfigForm from "./ConfigForm";

export default async function ConfigPage({
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
            We couldn&apos;t find your tenant in our database. This usually
            means the OAuth install didn&apos;t complete successfully.
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
          Configure your agent
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Set the system prompt and pick which tools the agent can use.
          You can change these any time by re-running the install link.
        </p>
      </header>

      <ConfigForm tenantId={tenantId} initial={tenant} />

      <footer className="mt-12 flex items-center justify-end gap-4 border-t border-[color:var(--border)] pt-6">
        <Link
          href={`/onboarding/${encodeURIComponent(tenantId)}/channels`}
          className="rounded-full border border-[color:var(--border)] px-5 py-2 text-sm font-medium hover:bg-[color:var(--card)]"
        >
          Next: channels →
        </Link>
      </footer>
    </div>
  );
}
