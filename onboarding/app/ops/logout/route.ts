/**
 * `/ops/logout` POST handler — clears the ops admin cookie.
 */
import { NextResponse, type NextRequest } from "next/server";

import { OPS_COOKIE_NAME } from "@/lib/ops";

export async function POST(request: NextRequest) {
  const proto = request.headers.get("x-forwarded-proto") ?? "https";
  const host =
    request.headers.get("x-forwarded-host") ?? request.headers.get("host") ?? "";
  const origin = host ? `${proto}://${host}` : request.url;

  const response = NextResponse.redirect(new URL("/ops/login", origin), {
    status: 302,
  });
  response.cookies.delete({
    name: OPS_COOKIE_NAME,
    path: "/ops",
  });
  return response;
}
