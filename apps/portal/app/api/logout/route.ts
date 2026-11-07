import { NextRequest, NextResponse } from "next/server";
import { COOKIE } from "@/lib/session";
import { sameHost } from "@/lib/url";

export async function POST(request: NextRequest) {
  const res = NextResponse.redirect(sameHost(request, "/login"), 303);
  res.cookies.delete(COOKIE);
  return res;
}
