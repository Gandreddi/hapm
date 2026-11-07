import { api, ONEVIEW_URL, requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

type EvalRow = { time: string; eval_id: string; agent_id: string; suite_id: string; aggregate: number; passed: boolean; kind?: string; run_id?: string };

export default async function EvalsPage() {
  const session = await requireSession();
  const { items } = await api<{ items: EvalRow[] }>(session, `${ONEVIEW_URL}/v1/evals?limit=100`);
  return (
    <>
      <h1>Eval scores</h1>
      <div className="table-wrap">
        <table data-testid="evals">
          <thead>
            <tr>
              <th>Time</th>
              <th>Agent</th>
              <th>Suite</th>
              <th>Kind</th>
              <th>Score</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {items.map((e) => (
              <tr key={e.eval_id}>
                <td>{new Date(e.time).toLocaleString()}</td>
                <td>{e.agent_id}</td>
                <td>{e.suite_id}</td>
                <td>{e.kind ?? "offline"}</td>
                <td>{e.aggregate.toFixed(2)}</td>
                <td className={e.passed ? "state-succeeded" : "state-failed"}>{e.passed ? "passed" : "failed"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
