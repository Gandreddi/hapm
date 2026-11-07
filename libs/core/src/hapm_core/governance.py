"""One place that checks policy, writes the audit row and emits events for every guarded action."""

from dataclasses import dataclass
from typing import Any

from hapm_events import SchemaRegistry, emit
from hapm_policy import Decision, PolicyEngine, PolicyRequest
from sqlalchemy import Connection, Engine

from hapm_core.artifacts import ArtifactStore
from hapm_core.audit import AuditEntry, record


@dataclass
class Governor:
    engine: Engine
    policy: PolicyEngine
    artifacts: ArtifactStore
    schemas: SchemaRegistry
    source: str

    def decide(
        self,
        actor: str,
        action: str,
        resource: str,
        *,
        attributes: dict[str, str] | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        inputs: Any = None,
    ) -> Decision:
        """Evaluate policy, then audit the decision and emit it, in one transaction."""
        decision = self.policy.evaluate(
            PolicyRequest(actor=actor, action=action, resource=resource, attributes=attributes or {})
        )
        with self.engine.begin() as conn:
            self.audit(
                conn,
                AuditEntry(
                    actor=actor,
                    action=f"policy.{action}",
                    resource=resource,
                    decision=decision.effect.value,
                    run_id=run_id,
                    task_id=task_id,
                    policy_rule_id=decision.rule_id,
                    inputs=inputs,
                ),
            )
            self.emit(
                conn,
                "run.policy_decided",
                {
                    "run_id": run_id,
                    "action": action,
                    "resource": resource,
                    "decision": decision.effect.value,
                    "rule_id": decision.rule_id,
                },
                subject=run_id,
            )
        return decision

    def audit(self, conn: Connection, entry: AuditEntry) -> dict[str, Any]:
        row = record(conn, entry, self.artifacts)
        self.emit(
            conn,
            "audit.recorded",
            {
                "audit_id": row["id"],
                "run_id": entry.run_id,
                "action": entry.action,
                "decision": entry.decision,
            },
            subject=entry.run_id,
        )
        return row

    def record_outcome(self, entry: AuditEntry) -> None:
        with self.engine.begin() as conn:
            self.audit(conn, entry)

    def emit(self, conn: Connection, event_type: str, data: dict[str, Any], subject: str | None = None) -> None:
        emit(conn, self.schemas, self.source, event_type, data, subject)
