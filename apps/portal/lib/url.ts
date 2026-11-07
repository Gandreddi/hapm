import { NextRequest } from "next/server";

/** Absolute URL on the host the browser used (request.url can show the container's bind address). */
export function sameHost(request: NextRequest, path: string): URL {
  const proto = request.headers.get("x-forwarded-proto") ?? "http";
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host") ?? "localhost:3000";
  return new URL(path, `${proto}://${host}`);
}
