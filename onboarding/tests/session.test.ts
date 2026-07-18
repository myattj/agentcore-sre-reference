import { createHmac } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { makeOpsSession, verifyOpsSession } from "@/lib/ops";
import {
  makeGitHubInstallState,
  verifyGitHubInstallState,
  verifySessionToken,
} from "@/lib/session";

const STATE_SECRET = "s".repeat(64);
const OTHER_SECRET = "o".repeat(64);
const NOW_SECONDS = 1_800_000_000;

function makeTenantSession(
  tenantId = "slack-t123",
  timestamp = NOW_SECONDS,
  secret = STATE_SECRET,
  timestampField = String(timestamp),
): string {
  const nonce = "a".repeat(32);
  const signature = createHmac("sha256", secret)
    .update(`${tenantId}.${nonce}.${timestamp}`)
    .digest("hex");
  return `${tenantId}.${nonce}.${timestampField}.${signature}`;
}

function tamperLastCharacter(value: string): string {
  return `${value.slice(0, -1)}${value.endsWith("0") ? "1" : "0"}`;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW_SECONDS * 1000);
  vi.stubEnv("BRIDGE_OAUTH_STATE_SECRET", STATE_SECRET);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe("tenant session verification", () => {
  it("accepts a valid bridge-compatible token", () => {
    expect(verifySessionToken(makeTenantSession())).toBe("slack-t123");
  });

  it.each([
    "",
    "one-part",
    "tenant.nonce.timestamp.signature.extra",
    makeTenantSession("slack-t123", NOW_SECONDS, STATE_SECRET, `${NOW_SECONDS}junk`),
  ])("rejects malformed input %#", (token) => {
    expect(verifySessionToken(token)).toBeNull();
  });

  it("rejects tampering, expiry, future minting, and the wrong secret", () => {
    const valid = makeTenantSession();
    expect(verifySessionToken(tamperLastCharacter(valid))).toBeNull();
    expect(
      verifySessionToken(makeTenantSession("slack-t123", NOW_SECONDS - 3_601)),
    ).toBeNull();
    expect(
      verifySessionToken(makeTenantSession("slack-t123", NOW_SECONDS + 31)),
    ).toBeNull();
    expect(
      verifySessionToken(
        makeTenantSession("slack-t123", NOW_SECONDS, OTHER_SECRET),
      ),
    ).toBeNull();
  });
});

describe("GitHub installation state", () => {
  it("is bound to the exact onboarding session", () => {
    const sessionA = makeTenantSession("slack-a");
    const sessionB = makeTenantSession("slack-b");
    const state = makeGitHubInstallState(sessionA);

    expect(verifyGitHubInstallState(state, sessionA)).toBe(true);
    expect(verifyGitHubInstallState(state, sessionB)).toBe(false);
    expect(verifyGitHubInstallState(tamperLastCharacter(state), sessionA)).toBe(
      false,
    );
  });

  it("rejects expired, future-dated, malformed, and secret-rotated state", () => {
    const session = makeTenantSession();
    const state = makeGitHubInstallState(session);

    vi.setSystemTime((NOW_SECONDS + 601) * 1000);
    expect(verifyGitHubInstallState(state, session)).toBe(false);

    vi.setSystemTime((NOW_SECONDS + 31) * 1000);
    const futureState = makeGitHubInstallState(session);
    vi.setSystemTime(NOW_SECONDS * 1000);
    expect(verifyGitHubInstallState(futureState, session)).toBe(false);

    const [nonce, timestamp, signature] = state.split(".");
    expect(
      verifyGitHubInstallState(`${nonce}.${timestamp}junk.${signature}`, session),
    ).toBe(false);
    vi.stubEnv("BRIDGE_OAUTH_STATE_SECRET", OTHER_SECRET);
    expect(verifyGitHubInstallState(state, session)).toBe(false);
  });
});

describe("operator sessions", () => {
  it("accepts a valid token without storing the raw secret", () => {
    const token = makeOpsSession(STATE_SECRET);
    expect(token).not.toContain(STATE_SECRET);
    expect(verifyOpsSession(token, STATE_SECRET)).toBe(true);
  });

  it("fails closed for malformed, tampered, expired, future, and rotated tokens", () => {
    const token = makeOpsSession(STATE_SECRET);
    expect(verifyOpsSession(tamperLastCharacter(token), STATE_SECRET)).toBe(false);
    expect(verifyOpsSession(token, OTHER_SECRET)).toBe(false);
    expect(verifyOpsSession(undefined, STATE_SECRET)).toBe(false);

    vi.setSystemTime((NOW_SECONDS + 3_601) * 1000);
    expect(verifyOpsSession(token, STATE_SECRET)).toBe(false);

    vi.setSystemTime((NOW_SECONDS + 31) * 1000);
    const futureToken = makeOpsSession(STATE_SECRET);
    vi.setSystemTime(NOW_SECONDS * 1000);
    expect(verifyOpsSession(futureToken, STATE_SECRET)).toBe(false);

    const [nonce, timestamp, signature] = token.split(".");
    expect(
      verifyOpsSession(`${nonce}.${timestamp}junk.${signature}`, STATE_SECRET),
    ).toBe(false);
  });
});
