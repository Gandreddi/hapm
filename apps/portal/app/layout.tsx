import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { canApprove, getSession } from "@/lib/session";

export const metadata: Metadata = { title: "HAPM OneView", description: "Read-only view of HAPM agent runs" };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await getSession();
  return (
    <html lang="en">
      <body>
        <header>
          <strong>HAPM OneView-lite</strong>
          {session && (
            <>
              <Link href="/runs">Runs</Link>
              <Link href="/evals">Evals</Link>
              {canApprove(session) && <Link href="/inbox">Approval inbox</Link>}
              <span className="who" data-testid="who">
                {session.sub} ({session.roles.join(", ")})
              </span>
              <form action="/api/logout" method="post">
                <button type="submit">Sign out</button>
              </form>
            </>
          )}
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
