import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/ops/logout/route";

beforeEach(() => {
  vi.stubEnv("ONBOARDING_PUBLIC_URL", "https://agent.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("operator logout", () => {
  it("redirects to the configured origin despite hostile forwarded headers", async () => {
    const request = new NextRequest("https://agent.test/ops/logout", {
      method: "POST",
      headers: {
        host: "evil.example",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "http",
      },
    });

    const response = await POST(request);

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe(
      "https://agent.test/ops/login",
    );
    expect(response.headers.get("set-cookie")).toContain("ops_session=");
    expect(response.headers.get("set-cookie")).toContain(
      "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    );
  });
});
