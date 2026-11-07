import Link from "next/link";
import { api, ONEVIEW_URL, requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

type RunRow = {
  run_id: string;
  state: string;
  agent_id: string;
  requester: string | null;
  mode: string | null;
  started_at: string;
  updated_at: string;
  cost_usd: number;
  llm_calls: number;
};

export default async function RunsPage() {
  const session = await requireSession();
  const { items } = await api<{ items: RunRow[] }>(session, `${ONEVIEW_URL}/v1/runs?limit=100`);
  return (
    <>
      <h1>Runs</h1>
      <p className="muted">Built from the event bus. Refresh to see new runs; open a run for its live timeline.</p>
      <div className="table-wrap">
        <table data-testid="runs-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Agent</th>
              <th>State</th>
              <th>Requester</th>
              <th>Mode</th>
              <th>LLM calls</th>
              <th>Cost (USD)</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <Link href={`/runs/${r.run_id}`}>{r.run_id.slice(0, 16)}…</Link>
                </td>
                <td>{r.agent_id}</td>
                <td className={`state-${r.state}`}>{r.state}</td>
                <td>{r.requester ?? "-"}</td>
                <td>{r.mode ?? "live"}</td>
                <td>{r.llm_calls}</td>
                <td>{r.cost_usd.toFixed(6)}</td>
                <td>{new Date(r.started_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length === 0 && <p className="muted">No runs yet.</p>}
    </>
  );
}
