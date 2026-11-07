"""Autonomy gating: an agent acts with approval only if its latest eval passed for this exact config."""

from typing import Any

from hapm_orchestrator.agents import AgentConfig, Autonomy, config_hash
from sqlalchemy import Connection, text


def latest_offline_eval(conn: Connection, agent_id: str, chash: str) -> Any:
    return conn.execute(
        text(
            "SELECT * FROM eval_results WHERE kind = 'offline' AND agent_id = :a AND config_hash = :h "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"a": agent_id, "h": chash},
    ).first()


def effective_autonomy(conn: Connection, agent: AgentConfig) -> tuple[Autonomy, str]:
    """Return the autonomy the agent may use now, with the reason."""
    if agent.autonomy is Autonomy.SUGGEST_ONLY:
        return Autonomy.SUGGEST_ONLY, "requested suggest_only"
    row = latest_offline_eval(conn, agent.id, config_hash(agent))
    if row is None:
        return Autonomy.SUGGEST_ONLY, "no eval has run for this agent configuration"
    if not row.passed or row.aggregate < agent.eval_threshold:
        return Autonomy.SUGGEST_ONLY, f"latest eval {row.aggregate:.2f} is below threshold {agent.eval_threshold:.2f}"
    return Autonomy.ACT_WITH_APPROVAL, f"eval {row.id} passed with {row.aggregate:.2f}"
