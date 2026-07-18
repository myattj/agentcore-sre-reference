import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BridgeApiError,
  connectConfluence,
  getOpsRoster,
} from "@/lib/bridge";

const SECRET_INPUT = "api-token-that-must-not-be-reflected";
const SAFE_DETAIL =
  "Value error, domain must be one safe Atlassian DNS label; Field required";

function validationErrorResponse(): Response {
  return new Response(
    JSON.stringify({
      detail: [
        {
          type: "value_error",
          loc: ["body", "domain"],
          msg: "Value error, domain must be one safe Atlassian DNS label",
          input: SECRET_INPUT,
        },
        {
          type: "missing",
          loc: ["body", "email"],
          msg: "Field required",
          input: { api_token: SECRET_INPUT },
        },
      ],
    }),
    {
      status: 422,
      statusText: "Unprocessable Entity",
      headers: { "content-type": "application/json" },
    },
  );
}

async function expectSafeBridgeError(call: () => Promise<unknown>): Promise<void> {
  try {
    await call();
    throw new Error("expected the bridge request to fail");
  } catch (error) {
    expect(error).toBeInstanceOf(BridgeApiError);
    expect(error).toMatchObject({ status: 422, detail: SAFE_DETAIL });
    expect(String(error)).not.toContain(SECRET_INPUT);
  }
}

beforeEach(() => {
  vi.stubEnv("BRIDGE_URL", "https://bridge.example.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("bridge error normalization", () => {
  it("normalizes FastAPI validation errors for tenant-session requests", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(validationErrorResponse()));

    await expectSafeBridgeError(() =>
      connectConfluence("slack-acme", "session-token", {
        email: "admin@example.test",
        api_token: "request-token",
        domain: "invalid.example.test",
      }),
    );
  });

  it("normalizes FastAPI validation errors for admin requests", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(validationErrorResponse()));

    await expectSafeBridgeError(() => getOpsRoster("admin-secret"));
  });
});
