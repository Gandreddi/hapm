import Link from "next/link";
import { api, canApprove, ORCHESTRATOR_URL, requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

type Approval = { id: string; run_id: string; agent_id: string; requester: string; what: string; risk: string; created_at: string; expires_at: string };

export default async function InboxPage() {
  const session = await requireSession();
  if (!canApprove(session)) return <p className="error">The approval inbox needs the approver role.</p>;
  const { items } = await api<{ items: Approval[] }>(session, `${ORCHESTRATOR_URL}/v1/approvals?status=pending`);
  return (
    <>
      <h1>Approval inbox</h1>
      <div className="table-wrap">
        <table data-testid="inbox">
          <thead>
            <tr>
              <th>What</th>
              <th>Agent</th>
              <th>Requested by</th>
              <th>Risk</th>
              <th>Expires</th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id} data-run={a.run_id}>
                <td>
                  <Link href={`/inbox/${a.id}`}>{a.what}</Link>
                </td>
                <td>{a.agent_id}</td>
                <td>{a.requester}</td>
                <td>{a.risk}</td>
                <td>{new Date(a.expires_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length === 0 && <p className="muted">Nothing waiting for approval.</p>}
    </>
  );
}
