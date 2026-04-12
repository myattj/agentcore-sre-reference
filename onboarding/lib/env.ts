/**
 * Typed env var accessor.
 *
 * Required vars throw on first read so misconfiguration surfaces at the
 * first request that needs them, with a descriptive message instead of
 * a generic `process.env.X is undefined` deep in some helper.
 *
 * Public vars (NEXT_PUBLIC_*) are read at build time and inlined into
 * the client bundle. Server-only vars are read at request time on the
 * server and never sent to the client.
 *
 * Required at runtime by every onboarding page that talks to the bridge:
 *   - BRIDGE_URL                       (server) base URL of the bridge API
 *   - BRIDGE_OAUTH_STATE_SECRET        (server) HMAC key shared with the bridge
 *   - NEXT_PUBLIC_BRIDGE_INSTALL_URL   (public) the /slack/install URL on the bridge
 *
 * The landing page only needs NEXT_PUBLIC_BRIDGE_INSTALL_URL.
 */

type RequiredEnvVar =
  | "BRIDGE_URL"
  | "BRIDGE_OAUTH_STATE_SECRET"
  | "NEXT_PUBLIC_BRIDGE_INSTALL_URL";

type OptionalEnvVar = "ADMIN_SECRET" | "GITHUB_APP_SLUG";

export function getEnv(name: RequiredEnvVar): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required environment variable: ${name}. ` +
        `See onboarding/.env.example for the full list.`,
    );
  }
  return value;
}

/** Server-only HMAC secret used to verify session tokens minted by the bridge. */
export function getStateSecret(): string {
  return getEnv("BRIDGE_OAUTH_STATE_SECRET");
}

/** Server-only base URL of the bridge for API calls. */
export function getBridgeUrl(): string {
  return getEnv("BRIDGE_URL").replace(/\/+$/, "");
}

/** Public URL of the Slack install endpoint, embedded in the landing page. */
export function getBridgeInstallUrl(): string {
  return getEnv("NEXT_PUBLIC_BRIDGE_INSTALL_URL");
}

/**
 * GitHub App slug for the AgentCore Reference codebase-access App.
 *
 * Returns ``null`` when unset — the onboarding UI should render a
 * disabled "GitHub App not configured" card instead of a broken link.
 * The slug is the URL-safe name of the App as it appears in its
 * github.com URL (e.g. the ``agentcore`` in ``github.com/apps/agentcore``).
 *
 * Setting this env var requires:
 *   1. Creating the GitHub App at github.com/settings/apps/new
 *   2. Configuring its Setup URL to
 *      ``https://<your-onboarding-host>/github/installed``
 *   3. Populating ``GITHUB_APP_ID`` (bridge-side) + the Secrets Manager
 *      secret ``agentcore/platform/github_app/private_key`` so the
 *      bridge can mint installation tokens during warm-start
 */
export function getGitHubAppSlug(): string | null {
  const v = process.env.GITHUB_APP_SLUG;
  return v && v.length > 0 ? v : null;
}


/**
 * Shared-secret admin token for the ``/ops`` operator dashboard.
 *
 * Returns ``null`` when unset — callers should treat that as "ops
 * disabled" and surface a helpful message rather than throwing. This
 * mirrors the bridge's ``_ops_guard`` which returns 503 when its own
 * ``ADMIN_SECRET`` is unset.
 *
 * Must match the value set on the bridge service; both the onboarding
 * cookie check and the bridge API guard compare against the same secret.
 */
export function getAdminSecret(): string | null {
  const v = process.env.ADMIN_SECRET;
  return v && v.length > 0 ? v : null;
}

// Suppress "unused type" for the narrowing token — kept exported so
// future optional envs can slot into the same alias.
export type { OptionalEnvVar };
