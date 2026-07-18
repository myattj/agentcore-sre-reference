/**
 * GitHub App install card — the codebase-access entry point.
 *
 * Distinct from `GitHubForm` (the PAT-based BYO integration). This card
 * drives the Agent GitHub App install flow:
 *
 *   1. User clicks "Install on GitHub"
 *   2. Link sends them to github.com/apps/{slug}/installations/new with
 *      a short-lived, session-bound CSRF state. The session bearer itself is
 *      never sent to GitHub.
 *   3. That handler POSTs to the bridge's warm-start endpoint and
 *      redirects back here with ?github=connected&repo=...
 *   4. On re-render, this card reads the tenant config and shows the
 *      connected state with the default repo + binding list
 *
 * When `GITHUB_APP_SLUG` env var is unset (the App hasn't been created
 * on github.com yet), the card renders a disabled "not yet configured"
 * state instead of a broken link. Same for a missing session token.
 *
 * Server component — reads session + env at request time, no client
 * state. The "install" interaction is a plain `<a>` link, not a form
 * submission, so no client-side JS needed.
 */
import type { CodebasesConfig } from "@/lib/types";

export function GitHubAppCard({
  codebases,
  appSlug,
  installState,
}: {
  codebases: CodebasesConfig | undefined;
  /** From `getGitHubAppSlug()` — null when unset. */
  appSlug: string | null;
  /** Purpose-specific, short-lived state bound to the current session. */
  installState: string;
}) {
  const connected = codebases?.enabled === true;
  const defaultRepo = codebases?.default_repo ?? null;
  const bindings = codebases?.bindings ?? [];

  // Connected state — show the default repo and the ranked shortlist.
  if (connected) {
    return (
      <div className="rounded-lg border border-green-200 bg-green-50 p-5">
        <div className="mb-2 flex items-start justify-between">
          <div className="min-w-0 flex-1">
            <h3 className="mb-1 font-semibold">GitHub App (Codebase access)</h3>
            <p className="text-xs text-[color:var(--muted)]">
              Your bot can read code across your repos and learn which
              codebase each channel maps to.
            </p>
          </div>
          <span className="ml-4 shrink-0 rounded-full border border-green-300 bg-green-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-green-700">
            Connected
          </span>
        </div>
        {defaultRepo ? (
          <p className="mt-3 text-xs">
            <span className="text-[color:var(--muted)]">Primary codebase: </span>
            <code className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px]">
              {defaultRepo}
            </code>
          </p>
        ) : (
          <p className="mt-3 text-xs text-[color:var(--muted)]">
            No default repo yet — add one from the integrations page after
            connecting.
          </p>
        )}
        {bindings.length > 0 && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer text-[color:var(--muted)]">
              {bindings.length} {bindings.length === 1 ? "repo" : "repos"} in
              your shortlist
            </summary>
            <ul className="mt-2 space-y-1">
              {bindings.map((b) => (
                <li key={b.repo}>
                  <code className="font-mono text-[11px]">{b.repo}</code>
                  <span className="ml-2 text-[color:var(--muted)]">
                    ({b.default_branch})
                  </span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    );
  }

  // Not-yet-configured state — App slug env var missing. Shouldn't
  // happen in production but catches local-dev misconfiguration.
  if (!appSlug) {
    return (
      <div className="rounded-lg border border-[color:var(--border)] bg-white p-5 opacity-60">
        <h3 className="mb-1 font-semibold">GitHub App (Codebase access)</h3>
        <p className="mb-3 text-xs text-[color:var(--muted)]">
          Your bot can read code across your repos and learn which codebase
          each channel maps to.
        </p>
        <p className="text-xs text-[color:var(--muted)]">
          Not available yet — the operator needs to set{" "}
          <code className="font-mono text-[11px]">GITHUB_APP_SLUG</code> and
          create the GitHub App on github.com first.
        </p>
      </div>
    );
  }

  // Fresh install — link out to github.com. `state` is a dedicated signed
  // nonce, not the tenant session bearer.
  const installUrl = `https://github.com/apps/${encodeURIComponent(appSlug)}/installations/new?state=${encodeURIComponent(installState)}`;

  return (
    <div className="rounded-lg border border-[color:var(--border)] bg-white p-5">
      <h3 className="mb-1 font-semibold">GitHub App (Codebase access)</h3>
      <p className="mb-3 text-xs text-[color:var(--muted)]">
        Install the Agent app on your GitHub org so the bot can read code
        across your repos. The deployment operator must approve the resulting
        installation ID for this tenant before repository access activates.
      </p>
      <a
        href={installUrl}
        className="inline-block rounded-full bg-[color:var(--accent)] px-4 py-1.5 text-xs font-medium text-white hover:bg-[color:var(--accent-hover)]"
      >
        Install on GitHub &rarr;
      </a>
    </div>
  );
}
