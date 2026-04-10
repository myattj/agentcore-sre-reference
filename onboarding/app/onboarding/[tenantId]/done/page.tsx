/**
 * Done page — onboarding complete.
 *
 * Static instructions: invite the bot to a channel, mention it, you're
 * done. No "complete" flag is written to the tenant row this week —
 * "is the user done?" can be inferred later from audit_log invocations.
 */
import Link from "next/link";

import { requireSession } from "@/lib/session";

export default async function DonePage({
  params,
}: {
  params: Promise<{ tenantId: string }>;
}) {
  const { tenantId } = await params;
  await requireSession(tenantId);

  return (
    <div>
      <header className="mb-8">
        <h1 className="mb-2 text-3xl font-semibold tracking-tight">
          You&apos;re ready
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Your tenant is provisioned. Here&apos;s how to take it for a spin.
        </p>
      </header>

      <ol className="space-y-4">
        <Step
          number={1}
          title="Open Slack"
          body="Go to your workspace where you installed agent-core."
        />
        <Step
          number={2}
          title="Invite the bot"
          body={
            <>
              Open any channel and type{" "}
              <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                /invite @agent-core
              </code>
              . The bot only sees channels it&apos;s been invited to.
            </>
          }
        />
        <Step
          number={3}
          title="Mention the bot"
          body={
            <>
              In that channel, type{" "}
              <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                @agent-core hi
              </code>
              . The first reply may take a few seconds while the agent warms
              up.
            </>
          }
        />
        <Step
          number={4}
          title="Iterate"
          body={
            <>
              Reply not quite right? Come back to{" "}
              <Link
                href={`/onboarding/${encodeURIComponent(tenantId)}/config`}
                className="underline"
              >
                Configure
              </Link>{" "}
              and tweak the system prompt. Changes take effect on the next
              message.
            </>
          }
        />
      </ol>

      <div className="mt-12 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-6">
        <h3 className="mb-2 text-sm font-semibold">What&apos;s next</h3>
        <p className="text-sm text-[color:var(--muted)]">
          Pattern 1 (alert triage with Datadog/PagerDuty/GitHub) and channel
          personas land in the next release. We&apos;ll email you when they
          ship.
        </p>
      </div>
    </div>
  );
}

function Step({
  number,
  title,
  body,
}: {
  number: number;
  title: string;
  body: React.ReactNode;
}) {
  return (
    <li className="flex gap-4 rounded-lg border border-[color:var(--border)] bg-white p-4">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[color:var(--accent)]/10 font-mono text-sm font-semibold text-[color:var(--accent)]">
        {number}
      </div>
      <div className="min-w-0 flex-1">
        <h3 className="mb-1 font-medium">{title}</h3>
        <p className="text-sm text-[color:var(--muted)]">{body}</p>
      </div>
    </li>
  );
}
