import Link from "next/link";
import { LiveTimeline, type TimelineItem } from "@/components/LiveTimeline";
import { api, AUDIT_PUBLIC_URL, ONEVIEW_URL, requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

type Timeline = {
  run_id: string;
  summary: {
    state: string | null;
    cost_usd: number;
    llm_calls: number;
    tool_calls: number;
    memory_accesses: number;
    policy_decisions: number;
    approvals: number;
    audit_rows: number;
  };
  items: TimelineItem[];
};

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const session = await requireSession();
  let timeline: Timeline;
  try {
    timeline = await api<Timeline>(session, `${ONEVIEW_URL}/v1/runs/${encodeURIComponent(id)}/timeline`);
  } catch {
    return <p className="muted">No events for run {id} yet. Refresh in a moment.</p>;
  }
  const s = timeline.summary;
  const waiting = timeline.items.filter((i) => i.type === "approval.requested").at(-1);
  return (
    <>
      <p>
        <Link href="/runs">← All runs</Link>
      </p>
      <h1>Run {id}</h1>
      <div className="card stats" data-testid="run-summary">
        <div>
          State<b className={`state-${s.state}`} data-testid="run-state">{s.state}</b>
        </div>
        <div>LLM calls<b>{s.llm_calls}</b></div>
        <div>Tool calls<b>{s.tool_calls}</b></div>
        <div>Memory<b>{s.memory_accesses}</b></div>
        <div>Policy decisions<b>{s.policy_decisions}</b></div>
        <div>Approvals<b>{s.approvals}</b></div>
        <div>Cost (USD)<b>{s.cost_usd.toFixed(6)}</b></div>
        <div>
          Audit rows<b>{s.audit_rows}</b>
          <a href={`${AUDIT_PUBLIC_URL}/v1/audit?run_id=${encodeURIComponent(id)}`} target="_blank" rel="noreferrer">
            audit trail
          </a>
        </div>
      </div>
      {s.state === "waiting_approval" && waiting && (
        <div className="card" data-testid="waiting-approval">
          <strong>Waiting for approval:</strong> {String(waiting.data.what)}
          <pre>{JSON.stringify(waiting.data.proposed_action, null, 2)}</pre>
        </div>
      )}
      <LiveTimeline runId={id} initial={timeline.items} />
    </>
  );
}
