import { NextRequest, NextResponse } from "next/server";
import { COOKIE, OIDC_TOKEN_URL } from "@/lib/session";
import { sameHost } from "@/lib/url";

export async function POST(request: NextRequest) {
  const form = await request.formData();
  const body = new URLSearchParams({
    grant_type: "password",
    username: String(form.get("username") ?? ""),
    password: String(form.get("password") ?? ""),
  });
  const resp = await fetch(OIDC_TOKEN_URL, { method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  if (!resp.ok) return NextResponse.redirect(sameHost(request, "/login?error=1"), 303);
  const { access_token, expires_in } = (await resp.json()) as { access_token: string; expires_in: number };
  const res = NextResponse.redirect(sameHost(request, "/runs"), 303);
  res.cookies.set(COOKIE, access_token, { httpOnly: true, sameSite: "lax", path: "/", maxAge: expires_in });
  return res;
}
