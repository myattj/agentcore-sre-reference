import { createHmac } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  cookieGet: vi.fn(),
  installGitHubApp: vi.fn(),
  redirect: vi.fn((url: string) => {
    throw { digest: `NEXT_REDIRECT;${url}`, url };
  }),
}));

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: mocks.cookieGet })),
}));
vi.mock("next/navigation", () => ({ redirect: mocks.redirect }));
vi.mock("@/lib/bridge", () => ({
  BridgeApiError: class BridgeApiError extends Error {
    detail: string;
    constructor(detail: string) {
      super(detail);
      this.detail = detail;
    }
  },
  installGitHubApp: mocks.installGitHubApp,
}));

import { GET } from "@/app/github/installed/route";
import { makeGitHubInstallState } from "@/lib/session";

const SECRET = "s".repeat(64);
const NOW_SECONDS = 1_800_000_000;

function sessionFor(tenantId: string): string {
  const nonce = "b".repeat(32);
  const signature = createHmac("sha256", SECRET)
    .update(`${tenantId}.${nonce}.${NOW_SECONDS}`)
    .digest("hex");
  return `${tenantId}.${nonce}.${NOW_SECONDS}.${signature}`;
}

function request(query: string) {
  return {
    nextUrl: new URL(`https://agent.test/github/installed?${query}`),
  } as Parameters<typeof GET>[0];
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW_SECONDS * 1000);
  vi.stubEnv("BRIDGE_OAUTH_STATE_SECRET", SECRET);
  mocks.cookieGet.mockReset();
  mocks.installGitHubApp.mockReset();
  mocks.redirect.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe("GitHub installation callback", () => {
  it("rejects a callback with no onboarding session", async () => {
    mocks.cookieGet.mockReturnValue(undefined);

    await expect(
      GET(request("installation_id=123&setup_action=install&state=anything")),
    ).rejects.toMatchObject({ url: "/onboarding/error?reason=no_session" });
    expect(mocks.installGitHubApp).not.toHaveBeenCalled();
  });

  it("rejects state minted for a different session", async () => {
    const sessionA = sessionFor("slack-a");
    const sessionB = sessionFor("slack-b");
    const stateA = makeGitHubInstallState(sessionA);
    mocks.cookieGet.mockReturnValue({ value: sessionB });

    await expect(
      GET(
        request(
          `installation_id=123&setup_action=install&state=${encodeURIComponent(stateA)}`,
        ),
      ),
    ).rejects.toMatchObject({
      url: "/onboarding/slack-b/integrations?github=error&reason=state_mismatch",
    });
    expect(mocks.installGitHubApp).not.toHaveBeenCalled();
  });

  it("warms the exact tenant only after session and state validation", async () => {
    const session = sessionFor("slack-a");
    const state = makeGitHubInstallState(session);
    mocks.cookieGet.mockReturnValue({ value: session });
    mocks.installGitHubApp.mockResolvedValue({ ok: true, pending_approval: true });

    await expect(
      GET(
        request(
          `installation_id=123&setup_action=install&state=${encodeURIComponent(state)}`,
        ),
      ),
    ).rejects.toMatchObject({
      url: "/onboarding/slack-a/integrations?github=pending&installation_id=123",
    });
    expect(mocks.installGitHubApp).toHaveBeenCalledWith("slack-a", session, "123");
  });
});
