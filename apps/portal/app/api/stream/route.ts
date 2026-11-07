import { NextRequest } from "next/server";
import { getSession, ONEVIEW_URL } from "@/lib/session";

export const dynamic = "force-dynamic";

/** Proxies OneView's server-sent events so the browser never handles the bearer token. */
export async function GET(request: NextRequest) {
  const session = await getSession();
  if (!session) return new Response("unauthorised", { status: 401 });
  const runId = request.nextUrl.searchParams.get("run_id");
  const url = new URL(`${ONEVIEW_URL}/v1/stream`);
  if (runId) url.searchParams.set("run_id", runId);
  const upstream = await fetch(url, { headers: { Authorization: `Bearer ${session.token}` }, signal: request.signal });
  if (!upstream.ok || !upstream.body) return new Response("stream unavailable", { status: 502 });
  return new Response(upstream.body, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" },
  });
}
