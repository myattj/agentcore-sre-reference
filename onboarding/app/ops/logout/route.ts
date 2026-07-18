/**
 * `/ops/logout` POST handler — clears the ops admin cookie.
 */
import { NextResponse, type NextRequest } from "next/server";

import { getOnboardingPublicOrigin } from "@/lib/env";
import { OPS_COOKIE_NAME } from "@/lib/ops";

export async function POST(_request: NextRequest) {
  const response = NextResponse.redirect(
    new URL("/ops/login", getOnboardingPublicOrigin()),
    { status: 302 },
  );
  response.cookies.delete({
    name: OPS_COOKIE_NAME,
    path: "/ops",
  });
  return response;
}
