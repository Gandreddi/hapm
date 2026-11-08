"""The single agent_run workflow: plan and execute through LangGraph, pause at approval gates, finish.

Workflow code must be deterministic, so it performs no I/O itself; activities do all of it.
A run always ends in exactly one terminal state: succeeded, failed, rejected or cancelled.
"""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

GRAPH_OPTIONS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(minutes=10),
    "heartbeat_timeout": timedelta(seconds=15),
    "retry_policy": RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=20),
        maximum_attempts=6,
    ),
}
SHORT_OPTIONS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(seconds=60),
    "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=10),
}


@workflow.defn(name="agent_run")
class AgentRunWorkflow:
    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None
        self._state = "queued"

    @workflow.signal
    def decide(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @workflow.query
    def state(self) -> str:
        return self._state

    async def _finish(
        self, run_id: str, state: str, output: dict[str, Any] | None = None, error: str | None = None
    ) -> str:
        await workflow.execute_activity(
            "finalize", {"run_id": run_id, "state": state, "output": output, "error": error}, **SHORT_OPTIONS
        )
        self._state = state
        return state

    @workflow.run
    async def run(self, run_id: str) -> str:
        try:
            await workflow.execute_activity("mark_running", run_id, **SHORT_OPTIONS)
            self._state = "running"
            resume: dict[str, Any] | None = None
            while True:
                result = await workflow.execute_activity(
                    "run_graph", {"run_id": run_id, "resume": resume}, **GRAPH_OPTIONS
                )
                if result["status"] != "interrupted":
                    return await self._finish(run_id, result["status"], result.get("output"), result.get("error"))
                approval = result["approval"]
                approval_id = await workflow.execute_activity(
                    "create_approval", {"run_id": run_id, "approval": approval}, **SHORT_OPTIONS
                )
                self._state = "waiting_approval"
                self._decision = None
                try:
                    await workflow.wait_condition(
                        lambda: self._decision is not None, timeout=timedelta(seconds=approval["timeout_seconds"])
                    )
                except TimeoutError:
                    await workflow.execute_activity("expire_approval", approval_id, **SHORT_OPTIONS)
                    return await self._finish(run_id, "rejected", error="approval timed out")
                decision = self._decision or {}
                if decision.get("decision") != "approve":
                    resume = {"decision": "reject"}
                    await workflow.execute_activity("run_graph", {"run_id": run_id, "resume": resume}, **GRAPH_OPTIONS)
                    return await self._finish(run_id, "rejected", error=f"rejected by {decision.get('decided_by')}")
                await workflow.execute_activity("mark_running", run_id, **SHORT_OPTIONS)
                self._state = "running"
                resume = decision
        except asyncio.CancelledError:
            # Cleanup must still run after cancellation, so shield it from the cancel.
            await asyncio.shield(asyncio.ensure_future(self._finish(run_id, "cancelled", error="cancelled by user")))
            raise
        except ActivityError as exc:
            cause = exc.cause.message if exc.cause is not None and hasattr(exc.cause, "message") else str(exc)
            return await self._finish(run_id, "failed", error=str(cause)[:500])
