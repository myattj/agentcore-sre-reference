/**
 * Done page — onboarding complete.
 *
 * Static instructions: invite the bot to a channel, mention it, you're
 * done. Includes a single link to `/workspace/{id}` for anyone who
 * wants to tune things after the fact — no in-wizard customization
 * steps.
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
        <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-[color:var(--muted)]">
          Step 2 of 2
        </p>
        <h1 className="mb-2 text-3xl font-semibold tracking-tight">
          You&apos;re ready
        </h1>
        <p className="text-sm text-[color:var(--muted)]">
          Your bot is provisioned with sensible defaults — all tools
          enabled, shared memory across channels, and a prompt that
          already knows how to triage alerts, answer questions, and do
          on-call handoffs. Here&apos;s how to take it for a spin.
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
              . The bot sees channels it&apos;s been invited to, and
              shares memory across all of them so context carries over.
            </>
          }
        />
        <Step
          number={3}
          title="Mention the bot"
          body={
            <>
              In that channel, try{" "}
              <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                @agent-core what&apos;s open?
              </code>{" "}
              or{" "}
              <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                @agent-core catch me up on this thread
              </code>
              . The first reply may take a few seconds while the agent
              warms up.
            </>
          }
        />
        <Step
          number={4}
          title="Talk to the bot to configure it"
          body={
            <>
              Need to add a trusted bot, remember a team fact, or
              restrict a skill to one channel? Just tell the bot in
              Slack — e.g.{" "}
              <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                @agent-core remember that the data team uses Snowflake
              </code>
              . It edits its own config and the change persists
              immediately.
            </>
          }
        />
      </ol>

      <section className="mt-12 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-5">
        <h2 className="mb-1 text-sm font-semibold">Prefer a GUI?</h2>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          Most teams never come back here — the bot can edit its own
          config from Slack. But if you want to tune the prompt,
          per-channel overrides, skills, or bot policy by hand, the
          settings live in your workspace.
        </p>
        <Link
          href={`/workspace/${encodeURIComponent(tenantId)}`}
          className="inline-flex items-center gap-2 text-sm font-medium text-[color:var(--accent)] hover:text-[color:var(--accent-hover)]"
        >
          Open workspace settings &rarr;
        </Link>
      </section>
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
