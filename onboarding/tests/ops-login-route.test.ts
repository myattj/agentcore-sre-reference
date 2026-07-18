import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/ops/login/submit/route";
import { verifyOpsSession } from "@/lib/ops";

const ADMIN_SECRET = "operator-secret-that-is-long-and-random";

function loginRequest(
  secret: string,
  headers: Record<string, string> = {},
): NextRequest {
  return new NextRequest("https://agent.test/ops/login/submit", {
    method: "POST",
    body: new URLSearchParams({ secret }),
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      ...headers,
    },
  });
}

beforeEach(() => {
  vi.stubEnv("ADMIN_SECRET", ADMIN_SECRET);
  vi.stubEnv("ONBOARDING_PUBLIC_URL", "https://agent.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("operator login", () => {
  it("does not create a session for an invalid shared secret", async () => {
    const response = await POST(loginRequest("wrong"));

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("https://agent.test/ops/login?e=1");
    expect(response.cookies.get("ops_session")).toBeUndefined();
  });

  it("sets a scoped, signed cookie for a valid shared secret", async () => {
    const response = await POST(loginRequest(ADMIN_SECRET));
    const cookie = response.cookies.get("ops_session");

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("https://agent.test/ops");
    expect(cookie).toBeDefined();
    expect(cookie?.value).not.toContain(ADMIN_SECRET);
    expect(verifyOpsSession(cookie?.value, ADMIN_SECRET)).toBe(true);
    expect(response.headers.get("set-cookie")).toContain("HttpOnly");
    expect(response.headers.get("set-cookie")?.toLowerCase()).toContain(
      "samesite=lax",
    );
    expect(response.headers.get("set-cookie")).toContain("Path=/ops");
  });

  it("keeps ops disabled when ADMIN_SECRET is absent", async () => {
    vi.stubEnv("ADMIN_SECRET", "");
    const response = await POST(loginRequest("anything"));

    expect(response.headers.get("location")).toBe("https://agent.test/ops/login?e=1");
    expect(response.cookies.get("ops_session")).toBeUndefined();
  });

  it("ignores hostile forwarded hosts when redirecting", async () => {
    const response = await POST(
      loginRequest(ADMIN_SECRET, {
        host: "evil.example",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "http",
      }),
    );

    expect(response.headers.get("location")).toBe("https://agent.test/ops");
  });
});
