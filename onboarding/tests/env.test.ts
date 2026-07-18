import { afterEach, describe, expect, it, vi } from "vitest";

import { getOnboardingPublicOrigin } from "@/lib/env";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("onboarding public origin", () => {
  it.each([
    ["https://agent.example.com/", "https://agent.example.com"],
    ["http://localhost:3000", "http://localhost:3000"],
    ["http://127.0.0.1:3000", "http://127.0.0.1:3000"],
  ])("accepts %s", (configured, expected) => {
    vi.stubEnv("ONBOARDING_PUBLIC_URL", configured);
    expect(getOnboardingPublicOrigin()).toBe(expected);
  });

  it.each([
    "not-a-url",
    "http://agent.example.com",
    "https://user:password@agent.example.com",
    "https://agent.example.com/ops",
    "https://agent.example.com/?next=evil",
    "javascript:alert(1)",
  ])("rejects an unsafe origin: %s", (configured) => {
    vi.stubEnv("ONBOARDING_PUBLIC_URL", configured);
    expect(() => getOnboardingPublicOrigin()).toThrow();
  });
});
