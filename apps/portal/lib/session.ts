import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export const COOKIE = "hapm_token";

export type Session = { token: string; sub: string; roles: string[] };

/** Decode the token payload for display only. Every backend API verifies the signature itself. */
function decode(token: string): { sub: string; roles: string[]; exp: number } | null {
  try {
    const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));
    return { sub: String(payload.sub), roles: Array.isArray(payload.roles) ? payload.roles : [], exp: Number(payload.exp) };
  } catch {
    return null;
  }
}

export async function getSession(): Promise<Session | null> {
  const token = (await cookies()).get(COOKIE)?.value;
  if (!token) return null;
  const claims = decode(token);
  if (!claims || claims.exp * 1000 < Date.now()) return null;
  return { token, sub: claims.sub, roles: claims.roles };
}

export async function requireSession(): Promise<Session> {
  const session = await getSession();
  if (!session) redirect("/login");
  return session;
}

export function canApprove(session: Session): boolean {
  return session.roles.includes("approver") || session.roles.includes("admin");
}

const env = (name: string, fallback: string) => process.env[name] ?? fallback;
export const ONEVIEW_URL = env("ONEVIEW_URL", "http://127.0.0.1:8005");
export const ORCHESTRATOR_URL = env("ORCHESTRATOR_URL", "http://127.0.0.1:8002");
export const OIDC_TOKEN_URL = env("OIDC_TOKEN_URL", "http://127.0.0.1:8006/token");
export const AUDIT_PUBLIC_URL = env("AUDIT_PUBLIC_URL", "http://127.0.0.1:8003");

export async function api<T>(session: Session, url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    ...init,
    cache: "no-store",
    headers: { ...(init?.headers ?? {}), Authorization: `Bearer ${session.token}`, "Content-Type": "application/json" },
  });
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
  return (await resp.json()) as T;
}
