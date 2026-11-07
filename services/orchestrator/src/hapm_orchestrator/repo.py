"""Database access for tasks, runs and approvals. State changes and events share a transaction."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from hapm_core.audit import AuditEntry
from hapm_core.governance import Governor
from sqlalchemy import Connection, text

TERMINAL = ("succeeded", "failed", "rejected", "cancelled")


def _emit_state(gov: Governor, conn: Connection, run: Any, state: str, **extra: Any) -> None:
    gov.emit(
        conn,
        "run.state_changed",
        {"run_id": run.id, "task_id": run.task_id, "agent_id": run.agent_id, "state": state, **extra},
        subject=run.id,
    )


def create_task_and_run(
    gov: Governor,
    *,
    task_id: str,
    run_id: str,
    agent_id: str,
    requester: str,
    task_input: dict[str, Any],
    mode: str,
    config_hash: str,
    autonomy: str,
) -> None:
    with gov.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tasks (id, agent_id, requester, input) VALUES (:i, :a, :r, CAST(:in AS jsonb))"),
            {"i": task_id, "a": agent_id, "r": requester, "in": json.dumps(task_input)},
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, task_id, agent_id, requester, state, mode, config_hash, autonomy) "
                "VALUES (:i, :t, :a, :r, 'queued', :m, :h, :au)"
            ),
            {"i": run_id, "t": task_id, "a": agent_id, "r": requester, "m": mode, "h": config_hash, "au": autonomy},
        )
        gov.audit(
            conn,
            AuditEntry(
                actor=f"user:{requester}",
                action="task.submit",
                resource=agent_id,
                decision="allow",
                run_id=run_id,
                task_id=task_id,
                inputs=task_input,
            ),
        )
        run = conn.execute(text("SELECT * FROM runs WHERE id = :i"), {"i": run_id}).one()
        _emit_state(gov, conn, run, "queued", requester=requester, mode=mode, autonomy=autonomy)


def get_run(conn: Connection, run_id: str) -> Any:
    return conn.execute(text("SELECT * FROM runs WHERE id = :i"), {"i": run_id}).first()


def get_task(conn: Connection, task_id: str) -> Any:
    return conn.execute(text("SELECT * FROM tasks WHERE id = :i"), {"i": task_id}).first()


def set_state(
    gov: Governor, run_id: str, state: str, *, output: dict[str, Any] | None = None, error: str | None = None
) -> bool:
    """Move a run to a new state. A terminal state can never be changed again (compare-and-set)."""
    with gov.engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE runs SET state = :s, output = COALESCE(CAST(:o AS jsonb), output), error = COALESCE(:e, error), "
                "updated_at = now() WHERE id = :i AND state NOT IN ('succeeded','failed','rejected','cancelled') RETURNING *"
            ),
            {"s": state, "o": json.dumps(output) if output is not None else None, "e": error, "i": run_id},
        ).first()
        if row is None:
            return False
        if state in TERMINAL:
            conn.execute(
                text("UPDATE approvals SET status = 'void' WHERE run_id = :i AND status = 'pending'"), {"i": run_id}
            )
            gov.audit(
                conn,
                AuditEntry(
                    actor="system:orchestrator",
                    action="run.finish",
                    resource=run_id,
                    decision=state,
                    run_id=run_id,
                    task_id=row.task_id,
                    outputs=output or {"error": error},
                ),
            )
        extra: dict[str, Any] = {}
        if output is not None:
            extra["cost_usd"] = output.get("cost_usd", 0)
            extra["steps"] = output.get("steps", 0)
        if error:
            extra["error"] = error
        _emit_state(gov, conn, row, state, **extra)
        return True


def create_approval(
    gov: Governor, approval_id: str, run_id: str, request: dict[str, Any], timeout_seconds: int
) -> None:
    with gov.engine.begin() as conn:
        run = get_run(conn, run_id)
        conn.execute(
            text(
                "INSERT INTO approvals (id, run_id, task_id, what, why, proposed_action, risk, preview, approval_policy_id, "
                "requested_by, expires_at) VALUES (:i, :r, :t, :what, :why, CAST(:pa AS jsonb), :risk, :pv, :pol, :by, :exp) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "i": approval_id,
                "r": run_id,
                "t": run.task_id,
                "what": request["what"],
                "why": request["why"],
                "pa": json.dumps(request["proposed_action"]),
                "risk": request["risk"],
                "pv": request["preview"],
                "pol": request["approval_policy_id"],
                "by": run.requester,
                "exp": datetime.now(UTC) + timedelta(seconds=timeout_seconds),
            },
        )
        conn.execute(
            text("UPDATE runs SET state = 'waiting_approval', updated_at = now() WHERE id = :i AND state = 'running'"),
            {"i": run_id},
        )
        gov.audit(
            conn,
            AuditEntry(
                actor=f"agent:{run.agent_id}",
                action="approval.request",
                resource=approval_id,
                decision="pending",
                run_id=run_id,
                task_id=run.task_id,
                inputs=request,
            ),
        )
        gov.emit(
            conn,
            "approval.requested",
            {
                "approval_id": approval_id,
                "run_id": run_id,
                "task_id": run.task_id,
                "what": request["what"],
                "why": request["why"],
                "proposed_action": request["proposed_action"],
                "risk": request["risk"],
                "preview": request["preview"],
                "expires_in_seconds": timeout_seconds,
            },
            subject=run_id,
        )
        _emit_state(gov, conn, run, "waiting_approval", approval_id=approval_id)


def expire_approval(gov: Governor, approval_id: str) -> None:
    with gov.engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE approvals SET status = 'expired', decision = 'reject', decided_by = 'system:timeout', "
                "decided_at = now() WHERE id = :i AND status = 'pending' RETURNING run_id, task_id"
            ),
            {"i": approval_id},
        ).first()
        if row:
            gov.audit(
                conn,
                AuditEntry(
                    actor="system:timeout",
                    action="approval.decide",
                    resource=approval_id,
                    decision="expired",
                    run_id=row.run_id,
                    task_id=row.task_id,
                ),
            )
            gov.emit(
                conn,
                "approval.decided",
                {
                    "approval_id": approval_id,
                    "run_id": row.run_id,
                    "decision": "expired",
                    "decided_by": "system:timeout",
                },
                subject=row.run_id,
            )


def mark_running(gov: Governor, run_id: str) -> None:
    with gov.engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE runs SET state = 'running', updated_at = now() WHERE id = :i AND state IN ('queued','waiting_approval') RETURNING *"
            ),
            {"i": run_id},
        ).first()
        if row:
            _emit_state(gov, conn, row, "running")
