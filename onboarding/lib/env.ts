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
