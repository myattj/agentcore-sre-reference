import { getBridgeInstallUrl } from "@/lib/env";

export default function LandingPage() {
  const installUrl = getBridgeInstallUrl();

  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-16">
      <div className="max-w-2xl text-center">
        <p className="mb-6 inline-block rounded-full border border-[color:var(--border)] bg-[color:var(--card)] px-4 py-1 text-xs font-medium uppercase tracking-wider text-[color:var(--muted)]">
          Internal-ops AI agent for Slack
        </p>
        <h1 className="mb-6 text-5xl font-semibold tracking-tight text-[color:var(--foreground)] sm:text-6xl">
          Triage alerts. Answer questions.
          <br />
          Automate the toil.
        </h1>
        <p className="mb-12 text-lg text-[color:var(--muted)]">
          Connect Agent to Slack, choose the tools each tenant may use, and
          turn an alert into evidence: metrics, runbooks, recent commits, a
          proposed fix, or a short-lived dashboard your team can share.
        </p>

        <a
          href={installUrl}
          className="inline-flex items-center gap-3 rounded-full bg-[color:var(--accent)] px-8 py-4 text-lg font-medium text-white shadow-lg transition hover:bg-[color:var(--accent-hover)]"
        >
          <SlackLogo />
          Add to Slack
        </a>

        <p className="mt-8 text-sm text-[color:var(--muted)]">
          You&apos;ll be redirected to Slack to authorize the install. This is a
          self-hosted reference implementation, so review the deployment and
          security guides before connecting a real workspace.
        </p>
      </div>

      <footer className="mt-24 flex gap-6 text-xs text-[color:var(--muted)]">
        <span>Agent</span>
        <span>·</span>
        <span>Open-source reference implementation</span>
      </footer>
    </main>
  );
}

function SlackLogo() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className="h-5 w-5"
      aria-hidden
    >
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.52-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.833 24a2.528 2.528 0 0 1-2.52-2.522v-6.313zM8.833 5.042a2.528 2.528 0 0 1-2.52-2.52A2.528 2.528 0 0 1 8.833 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.833zm0 1.271a2.527 2.527 0 0 1 2.521 2.52 2.527 2.527 0 0 1-2.521 2.521H2.522A2.527 2.527 0 0 1 0 8.833a2.527 2.527 0 0 1 2.522-2.52h6.311zm10.122 2.52a2.527 2.527 0 0 1 2.522-2.52A2.527 2.527 0 0 1 24 8.833a2.527 2.527 0 0 1-2.522 2.521h-2.523V8.833zm-1.268 0a2.527 2.527 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.164 0a2.528 2.528 0 0 1 2.523 2.522v6.311zm-2.523 10.122a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.164 24a2.527 2.527 0 0 1-2.52-2.522v-2.523h2.52zm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.164a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
    </svg>
  );
}
