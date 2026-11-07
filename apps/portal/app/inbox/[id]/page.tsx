import Link from "next/link";
import { redirect } from "next/navigation";
import { api, canApprove, ORCHESTRATOR_URL, requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

type Approval = {
  id: string;
  run_id: string;
  agent_id: string;
  requester: string;
  status: string;
  what: string;
  why: string;
  risk: string;
  preview: string;
  proposed_action: { tool: string; args: Record<string, string> };
  decided_by: string | null;
  comment: string | null;
};

async function submitDecision(decision: "approve" | "reject", formData: FormData) {
  const session = await requireSession();
  const id = String(formData.get("id"));
  const resp = await fetch(`${ORCHESTRATOR_URL}/v1/approvals/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    headers: { Authorization: `Bearer ${session.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ decision, comment: String(formData.get("comment") ?? "") }),
    cache: "no-store",
  });
  if (!resp.ok) {
    const detail = ((await resp.json().catch(() => ({}))) as { detail?: string }).detail ?? resp.statusText;
    redirect(`/inbox/${encodeURIComponent(id)}?error=${encodeURIComponent(detail)}`);
  }
  redirect(`/inbox/${encodeURIComponent(id)}`);
}

async function approve(formData: FormData) {
  "use server";
  await submitDecision("approve", formData);
}

async function reject(formData: FormData) {
  "use server";
  await submitDecision("reject", formData);
}

export default async function ApprovalPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ error?: string }> }) {
  const { id } = await params;
  const { error } = await searchParams;
  const session = await requireSession();
  if (!canApprove(session)) return <p className="error">The approval inbox needs the approver role.</p>;
  const a = await api<Approval>(session, `${ORCHESTRATOR_URL}/v1/approvals/${encodeURIComponent(id)}`);
  return (
    <>
      <p>
        <Link href="/inbox">← Inbox</Link> · <Link href={`/runs/${a.run_id}`}>Run timeline</Link>
      </p>
      <h1>{a.what}</h1>
      <div className="card">
        <p>
          <strong>Why:</strong> {a.why}
        </p>
        <p>
          <strong>Agent:</strong> {a.agent_id} · <strong>Requested by:</strong> {a.requester} · <strong>Risk:</strong> {a.risk}
        </p>
        <p>
          <strong>Status:</strong> <span data-testid="approval-status">{a.status}</span>
          {a.decided_by && ` by ${a.decided_by}`}
        </p>
        <h3>Preview of {a.proposed_action.tool}</h3>
        <pre data-testid="preview">{a.preview}</pre>
      </div>
      {error && <p className="error" data-testid="decision-error">{error}</p>}
      {a.status === "pending" && (
        <form action={approve} className="card inline">
          <input type="hidden" name="id" value={a.id} />
          <input name="comment" placeholder="Comment (optional)" aria-label="Comment" style={{ flex: 1, minWidth: 200 }} />
          <button className="primary" type="submit">
            Approve
          </button>
          <button formAction={reject} type="submit">
            Reject
          </button>
        </form>
      )}
    </>
  );
}
