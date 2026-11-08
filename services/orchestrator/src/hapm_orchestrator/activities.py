"""Temporal activities. All I/O happens here; the workflow only makes decisions."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_orchestrator import repo
from hapm_orchestrator.agents import AgentRegistry, Autonomy
from hapm_orchestrator.graph import Deps, build_graph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from sqlalchemy import text
from temporalio import activity

OnlineScorer = Callable[[str, dict[str, Any]], None]


@dataclass
class Activities:
    deps: Deps
    agents: AgentRegistry
    checkpoint_dsn: str
    online_scorer: OnlineScorer | None = None

    @property
    def gov(self) -> Governor:
        return self.deps.governor

    @activity.defn(name="mark_running")
    def mark_running(self, run_id: str) -> None:
        repo.mark_running(self.gov, run_id)

    @activity.defn(name="run_graph")
    def run_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        with self.gov.engine.connect() as conn:
            run = repo.get_run(conn, run_id)
            task = repo.get_task(conn, run.task_id)
        agent = self.agents.load(run.agent_id)
        ctx = CallerContext(agent_id=agent.id, team_id=agent.team_id, run_id=run.id, task_id=run.task_id, mode=run.mode)
        task_input = dict(task.input)
        options = dict(task_input.pop("options", {}) or {})
        graph = build_graph(agent, self.deps, ctx, Autonomy(run.autonomy), options)
        config = {"configurable": {"thread_id": run_id}}
        with PostgresSaver.from_conn_string(self.checkpoint_dsn) as saver:
            compiled = graph.compile(checkpointer=saver)
            snapshot = compiled.get_state(config)
            waiting = any(t.interrupts for t in snapshot.tasks)
            if payload.get("resume") is not None and waiting:
                start: Any = Command(resume=payload["resume"])
            elif snapshot.values:
                start = None  # retry after a crash: continue from the last checkpoint
            else:
                start = {"input": task_input}
            for _ in compiled.stream(start, config, stream_mode="updates"):
                activity.heartbeat()
            snapshot = compiled.get_state(config)
        interrupts = [i.value for t in snapshot.tasks for i in t.interrupts]
        if interrupts:
            return {"status": "interrupted", "approval": interrupts[0]}
        values = snapshot.values
        output = {
            "summary": next(
                (v for k, v in values.get("outputs", {}).items() if isinstance(v, str) and not v.startswith("mem_")),
                None,
            ),
            "outputs": values.get("outputs", {}),
            "tool_calls": values.get("tool_calls", []),
            "steps": values.get("steps_done", 0),
            "cost_usd": values.get("cost_usd", 0.0),
            "autonomy": run.autonomy,
        }
        errors = [v["error"] for v in values.get("outputs", {}).values() if isinstance(v, dict) and v.get("error")]
        return {"status": values.get("status", "succeeded"), "output": output, "error": errors[-1] if errors else None}

    @activity.defn(name="create_approval")
    def create_approval(self, payload: dict[str, Any]) -> str:
        approval_id = f"apr_{payload['run_id']}_{payload['approval']['step_id']}"
        repo.create_approval(
            self.gov, approval_id, payload["run_id"], payload["approval"], payload["approval"]["timeout_seconds"]
        )
        return approval_id

    @activity.defn(name="expire_approval")
    def expire_approval(self, approval_id: str) -> None:
        repo.expire_approval(self.gov, approval_id)

    @activity.defn(name="finalize")
    def finalize(self, payload: dict[str, Any]) -> bool:
        changed = repo.set_state(
            self.gov, payload["run_id"], payload["state"], output=payload.get("output"), error=payload.get("error")
        )
        if changed and self.online_scorer and payload.get("output") is not None:
            try:
                self.online_scorer(payload["run_id"], payload["output"])
            except Exception:
                activity.logger.exception("online scoring failed")
        return changed

    def all(self) -> list[Callable[..., Any]]:
        return [self.mark_running, self.run_graph, self.create_approval, self.expire_approval, self.finalize]


def run_cost(gov: Governor, run_id: str) -> float:
    with gov.engine.connect() as conn:
        return float(
            conn.execute(
                text("SELECT COALESCE(sum(cost_usd), 0) FROM llm_usage WHERE run_id = :r"), {"r": run_id}
            ).scalar()
        )
