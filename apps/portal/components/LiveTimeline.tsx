"use client";

import { useEffect, useState } from "react";

export type TimelineItem = { seq: number; type: string; kind: string; time: string; data: Record<string, unknown> };

function describe(item: TimelineItem): string {
  const d = item.data;
  switch (item.type) {
    case "run.state_changed":
      return `state → ${d.state}${d.error ? ` (${d.error})` : ""}`;
    case "run.step":
      return `step ${d.step} (${d.kind}) ${d.status}`;
    case "run.tool_called":
      return `tool ${d.tool} [${d.kind}] ${d.status}`;
    case "run.memory_accessed":
      return `memory ${d.operation} on ${d.scope}: ${d.allowed ? "allowed" : "denied"}`;
    case "run.policy_decided":
      return `policy ${d.action} on ${d.resource}: ${d.decision} (rule ${d.rule_id})`;
    case "approval.requested":
      return `approval requested: ${d.what}`;
    case "approval.decided":
      return `approval ${d.decision} by ${d.decided_by}`;
    case "llm.usage":
      return `LLM ${d.alias ?? ""} via ${d.provider}/${d.model}: ${d.prompt_tokens}+${d.completion_tokens} tokens, $${d.cost_usd}`;
    case "eval.completed":
      return `eval ${d.suite_id}: ${d.aggregate} (${d.passed ? "passed" : "failed"})`;
    default:
      return item.type;
  }
}

export function LiveTimeline({ runId, initial }: { runId: string; initial: TimelineItem[] }) {
  const [items, setItems] = useState(initial);
  const [live, setLive] = useState(false);

  useEffect(() => {
    const source = new EventSource(`/api/stream?run_id=${encodeURIComponent(runId)}`);
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    source.addEventListener("hapm", (e) => {
      const item = JSON.parse((e as MessageEvent).data) as TimelineItem;
      setItems((prev) => (prev.some((p) => p.seq === item.seq) ? prev : [...prev, item]));
    });
    return () => source.close();
  }, [runId]);

  return (
    <div className="card">
      <h2>
        Timeline <span className="badge">{live ? "live" : "connecting"}</span>
      </h2>
      <div className="table-wrap">
        <table data-testid="timeline">
          <thead>
            <tr>
              <th>Time</th>
              <th>Kind</th>
              <th>Event</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.seq} data-kind={i.kind}>
                <td>{new Date(i.time).toLocaleTimeString()}</td>
                <td>
                  <span className="badge">{i.kind}</span>
                </td>
                <td>{describe(i)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
