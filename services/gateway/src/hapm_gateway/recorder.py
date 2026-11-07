"""Policy checks plus usage, audit and event records for each gateway call."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from hapm_core.audit import AuditEntry
from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_policy import Decision, PolicyEngine, PolicyRequest
from sqlalchemy import Engine, text


@dataclass
class Usage:
    alias: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    status: str
    request: Any
    response: Any


class Recorder(Protocol):
    def decide(self, ctx: CallerContext, action: str, resource: str) -> Decision: ...

    def record(self, ctx: CallerContext, action: str, usage: Usage) -> None: ...


class GovernorRecorder:
    def __init__(self, governor: Governor) -> None:
        self.governor = governor

    def decide(self, ctx: CallerContext, action: str, resource: str) -> Decision:
        return self.governor.decide(ctx.actor, action, resource, run_id=ctx.run_id, task_id=ctx.task_id)

    def record(self, ctx: CallerContext, action: str, usage: Usage) -> None:
        with self.governor.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_usage (agent_id, team_id, run_id, task_id, alias, provider, model, "
                    "prompt_tokens, completion_tokens, cost_usd, latency_ms, status) VALUES (:agent, "
                    ":team, :run, :task, :alias, :provider, :model, :pt, :ct, :cost, :lat, :status)"
                ),
                {
                    "agent": ctx.agent_id,
                    "team": ctx.team_id,
                    "run": ctx.run_id,
                    "task": ctx.task_id,
                    "alias": usage.alias,
                    "provider": usage.provider,
                    "model": usage.model,
                    "pt": usage.prompt_tokens,
                    "ct": usage.completion_tokens,
                    "cost": usage.cost_usd,
                    "lat": usage.latency_ms,
                    "status": usage.status,
                },
            )
            self.governor.audit(
                conn,
                AuditEntry(
                    actor=ctx.actor,
                    action=f"{action}.completed",
                    resource=usage.alias,
                    decision=usage.status,
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    inputs=usage.request,
                    outputs=usage.response,
                ),
            )
            self.governor.emit(
                conn,
                "llm.usage",
                {
                    "run_id": ctx.run_id,
                    "agent_id": ctx.agent_id,
                    "team_id": ctx.team_id,
                    "provider": usage.provider,
                    "model": usage.model,
                    "alias": usage.alias,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cost_usd": usage.cost_usd,
                    "latency_ms": usage.latency_ms,
                    "status": usage.status,
                },
                subject=ctx.run_id,
            )


@dataclass
class MemoryRecorder:
    """For unit tests: real policy evaluation, records kept in lists."""

    policy: PolicyEngine
    decisions: list[tuple[str, str, Decision]] = field(default_factory=list)
    usages: list[tuple[str, Usage]] = field(default_factory=list)

    def decide(self, ctx: CallerContext, action: str, resource: str) -> Decision:
        d = self.policy.evaluate(PolicyRequest(actor=ctx.actor, action=action, resource=resource))
        self.decisions.append((action, resource, d))
        return d

    def record(self, ctx: CallerContext, action: str, usage: Usage) -> None:
        self.usages.append((action, usage))


def purge_usage(engine: Engine, retention_days: int) -> int:
    """Delete usage rows older than the retention period; returns how many were removed."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    with engine.begin() as conn:
        return conn.execute(text("DELETE FROM llm_usage WHERE ts < :c"), {"c": cutoff}).rowcount
