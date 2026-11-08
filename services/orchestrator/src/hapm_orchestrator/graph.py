"""Build a LangGraph graph from an agent's steps.

Steps reach the LLM, memory and tools only through the Core clients. Each tool step becomes two
nodes: check_<id> asks policy (and records the result in state), and act_<id> pauses with
interrupt() when approval is needed. Gate steps work the same way: prepare_<id> lets a plugin build
the approval request, and gate_<id> pauses. When LangGraph resumes, it re-runs only the pausing
node, so the policy check, the plugin and their audit rows are not repeated.
"""

import asyncio
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

import httpx
from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_mcp_client import ToolCallError, ToolClient, ToolResult
from hapm_memory import MemoryScope, MemoryStore
from hapm_orchestrator.agents import (
    AgentConfig,
    Autonomy,
    GateStep,
    LlmStep,
    MemoryWriteStep,
    PluginStep,
    ToolStep,
    render,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from temporalio.exceptions import ApplicationError

STOP_STATES = ("rejected", "failed")


class RunState(TypedDict, total=False):
    input: dict[str, Any]
    plan: list[str]
    outputs: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    steps_done: int
    cost_usd: float
    status: str
    pending: dict[str, Any] | None


@dataclass
class Deps:
    governor: Governor
    memory: MemoryStore
    tools: ToolClient
    gateway_url: str
    extras: dict[str, Any] = field(default_factory=dict)  # e.g. sandbox client, service token provider


@dataclass
class PluginContext:
    """What a plugin step may use. Everything external goes through these Core clients."""

    deps: Deps
    ctx: CallerContext
    agent: AgentConfig
    autonomy: Autonomy
    options: dict[str, Any]
    config: dict[str, str]
    step_id: str

    def call_tool(
        self, tool: str, args: dict[str, Any], *, approved: bool = False, key: str | None = None
    ) -> ToolResult:
        try:
            return asyncio.run(
                self.deps.tools.call(
                    self.ctx,
                    tool,
                    args,
                    self.agent.allowed_tools,
                    approved=approved,
                    idempotency_key=key or f"{self.ctx.run_id}:{self.step_id}:{tool}",
                )
            )
        except ToolCallError as exc:
            if exc.code == "unavailable":
                raise RuntimeError(str(exc)) from exc
            raise ApplicationError(f"tool {tool} failed: {exc.code}: {exc}", non_retryable=True) from exc

    def chat(self, prompt: str, alias: str | None = None, system: str | None = None) -> tuple[str, float]:
        return chat(
            self.deps,
            self.ctx,
            alias or self.agent.model_alias,
            system or self.agent.system_prompt,
            prompt,
            float(self.options.get("fake_delay_seconds", 0) or 0),
            None,
        )

    def event(self, status: str, **extra: Any) -> None:
        _step_event(self.deps, self.ctx, self.step_id, "plugin", status, **extra)


def _values(state: RunState) -> dict[str, Any]:
    return {"input": state.get("input", {}), "steps": state.get("outputs", {})}


def _step_event(deps: Deps, ctx: CallerContext, step: str, kind: str, status: str, **extra: Any) -> None:
    with deps.governor.engine.begin() as conn:
        deps.governor.emit(
            conn,
            "run.step",
            {
                "run_id": ctx.run_id or "",
                "task_id": ctx.task_id or "",
                "step": step,
                "kind": kind,
                "status": status,
                **extra,
            },
            subject=ctx.run_id,
        )


def chat(
    deps: Deps, ctx: CallerContext, alias: str, system: str, prompt: str, fake_delay: float, max_tokens: int | None
) -> tuple[str, float]:
    headers = ctx.headers()
    if fake_delay:
        headers["X-HAPM-Fake-Delay"] = str(fake_delay)
    body: dict[str, Any] = {
        "model": alias,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    try:
        resp = httpx.post(f"{deps.gateway_url}/v1/chat", json=body, headers=headers, timeout=120)
    except httpx.TransportError as exc:
        raise RuntimeError(f"gateway unreachable: {exc}") from exc
    if resp.status_code >= 500:
        raise RuntimeError(f"gateway error {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise ApplicationError(f"gateway refused the call ({resp.status_code}): {resp.text[:300]}", non_retryable=True)
    data = resp.json()
    return data["choices"][0]["message"]["content"], float(data["hapm"]["cost_usd"])


def call_llm(
    deps: Deps, ctx: CallerContext, agent: AgentConfig, step: LlmStep, prompt: str, fake_delay: float
) -> tuple[str, float]:
    return chat(
        deps, ctx, step.model_alias or agent.model_alias, agent.system_prompt, prompt, fake_delay, step.max_tokens
    )


def load_plugin(ref: str) -> Callable[..., Any]:
    module, _, name = ref.partition(":")
    fn = getattr(importlib.import_module(module), name)
    if not callable(fn):
        raise TypeError(f"plugin {ref} is not callable")
    return fn  # type: ignore[no-any-return]


def build_graph(
    agent: AgentConfig, deps: Deps, ctx: CallerContext, autonomy: Autonomy, options: dict[str, Any]
) -> StateGraph:
    graph = StateGraph(RunState)
    fake_delay = float(options.get("fake_delay_seconds", 0) or 0)

    def plan(state: RunState) -> RunState:
        plan_ids = [s.id for s in agent.steps]
        _step_event(deps, ctx, "plan", "plan", "completed", plan=plan_ids)
        return {
            "plan": plan_ids,
            "outputs": {},
            "tool_calls": [],
            "steps_done": 0,
            "cost_usd": 0.0,
            "status": "running",
            "pending": None,
        }

    def make_llm(step: LlmStep) -> Callable[[RunState], RunState]:
        def node(state: RunState) -> RunState:
            text, cost = call_llm(deps, ctx, agent, step, render(step.prompt, _values(state)), fake_delay)
            _step_event(deps, ctx, step.id, "llm", "completed")
            return {
                "outputs": {**state["outputs"], step.id: text},
                "steps_done": state["steps_done"] + 1,
                "cost_usd": round(state["cost_usd"] + cost, 6),
            }

        return node

    def make_memory(step: MemoryWriteStep) -> Callable[[RunState], RunState]:
        def node(state: RunState) -> RunState:
            record_id = deps.memory.write(
                ctx,
                MemoryScope(step.scope),
                render(step.content, _values(state)),
                list(agent.memory_scopes),
                ttl_seconds=step.ttl_seconds,
            )
            _step_event(deps, ctx, step.id, "memory_write", "completed")
            return {"outputs": {**state["outputs"], step.id: record_id}, "steps_done": state["steps_done"] + 1}

        return node

    def tool_call(step: ToolStep, args: dict[str, Any], approved: bool) -> Any:
        pctx = PluginContext(deps, ctx, agent, autonomy, options, {}, step.id)
        return pctx.call_tool(step.tool, args, approved=approved, key=f"{ctx.run_id}:{step.id}")

    def record_call(state: RunState, step: ToolStep, args: dict[str, Any], status: str, output: Any) -> RunState:
        call = {"step": step.id, "tool": step.tool, "args": args, "status": status, "output": output}
        return {
            "tool_calls": [*state["tool_calls"], call],
            "outputs": {**state["outputs"], step.id: output},
            "steps_done": state["steps_done"] + 1,
            "pending": None,
        }

    def approval_request(step_id: str, policy_id: str, request: dict[str, Any]) -> dict[str, Any]:
        policy = deps.governor.policy.approval_policy(policy_id)
        timeout = min(policy.timeout_seconds, int(options.get("approval_timeout_seconds") or policy.timeout_seconds))
        return {"step_id": step_id, "approval_policy_id": policy_id, "timeout_seconds": timeout, **request}

    def make_check(step: ToolStep) -> Callable[[RunState], RunState]:
        def node(state: RunState) -> RunState:
            args = {k: render(v, _values(state)) for k, v in step.args.items()}
            result = tool_call(step, args, approved=False)
            if result.status in ("ok", "dry_run"):
                _step_event(deps, ctx, step.id, "tool", result.status)
                return record_call(state, step, args, result.status, result.output)
            if autonomy is Autonomy.SUGGEST_ONLY:
                _step_event(deps, ctx, step.id, "tool", "suggested")
                return record_call(state, step, args, "suggested", None)
            preview = "\n".join(f"{k}: {v}" for k, v in args.items())
            return {
                "pending": approval_request(
                    step.id,
                    result.approval_policy_id or agent.approval_policy,
                    {
                        "tool": step.tool,
                        "args": args,
                        "what": f"Run write tool {step.tool}",
                        "why": step.description or f"Step {step.id} of {agent.name}",
                        "proposed_action": {"tool": step.tool, "args": args, "kind": result.kind},
                        "risk": "medium: writes to an external system",
                        "preview": preview,
                    },
                )
            }

        return node

    def make_act(step: ToolStep) -> Callable[[RunState], RunState]:
        def node(state: RunState) -> RunState:
            pending = state.get("pending")
            if not pending or pending["step_id"] != step.id:
                return {}
            decision = interrupt(pending)
            if decision.get("decision") != "approve":
                _step_event(deps, ctx, step.id, "tool", "rejected")
                return {"status": "rejected", "pending": None}
            args = {**pending["args"], **(decision.get("edited_args") or {})}
            result = tool_call(step, args, approved=True)
            _step_event(deps, ctx, step.id, "tool", result.status)
            return record_call(state, step, args, result.status, result.output)

        return node

    def make_plugin(step: PluginStep) -> Callable[[RunState], RunState]:
        fn = load_plugin(step.plugin)

        def node(state: RunState) -> RunState:
            if state.get("status") in STOP_STATES:
                return {}
            pctx = PluginContext(deps, ctx, agent, autonomy, options, step.config, step.id)
            result: dict[str, Any] = fn(pctx, state) or {}
            _step_event(deps, ctx, step.id, "plugin", result.get("status", "completed"))
            update: RunState = {
                "outputs": {**state["outputs"], step.id: result.get("output")},
                "steps_done": state["steps_done"] + 1,
                "cost_usd": round(state["cost_usd"] + float(result.get("cost_usd", 0)), 6),
            }
            if result.get("tool_calls"):
                update["tool_calls"] = [*state["tool_calls"], *result["tool_calls"]]
            if result.get("status") in STOP_STATES:
                update["status"] = result["status"]
            return update

        return node

    def make_prepare(step: GateStep) -> Callable[[RunState], RunState]:
        fn = load_plugin(step.plugin)

        def node(state: RunState) -> RunState:
            if state.get("status") in STOP_STATES:
                return {"pending": None}
            pctx = PluginContext(deps, ctx, agent, autonomy, options, {"gate": step.gate}, step.id)
            request = fn(pctx, state)
            if request is None:
                _step_event(deps, ctx, step.id, "gate", "skipped")
                return {"pending": None, "outputs": {**state["outputs"], step.id: {"decision": "skipped"}}}
            policy_id = request.pop("approval_policy_id", agent.approval_policy)
            return {"pending": approval_request(step.id, policy_id, request)}

        return node

    def make_gate(step: GateStep) -> Callable[[RunState], RunState]:
        def node(state: RunState) -> RunState:
            pending = state.get("pending")
            if not pending or pending["step_id"] != step.id:
                return {}
            if ctx.mode == "eval":
                # Eval runs never wait for people; every write after this gate is dry-run by policy.
                _step_event(deps, ctx, step.id, "gate", "eval_dry_run")
                return {
                    "pending": None,
                    "outputs": {**state["outputs"], step.id: {"decision": "eval_dry_run", "request": pending}},
                }
            decision = interrupt(pending)
            if decision.get("decision") != "approve":
                _step_event(deps, ctx, step.id, "gate", "rejected")
                return {"status": "rejected", "pending": None, "outputs": {**state["outputs"], step.id: decision}}
            _step_event(deps, ctx, step.id, "gate", "approved")
            return {"pending": None, "outputs": {**state["outputs"], step.id: decision}}

        return node

    def finish(state: RunState) -> RunState:
        status = state.get("status") if state.get("status") in STOP_STATES else "succeeded"
        _step_event(deps, ctx, "finish", "finish", str(status))
        return {"status": str(status)}

    chain: list[str] = ["plan"]
    graph.add_node("plan", plan)
    for step in agent.steps:
        if isinstance(step, LlmStep):
            graph.add_node(step.id, make_llm(step))
            chain.append(step.id)
        elif isinstance(step, MemoryWriteStep):
            graph.add_node(step.id, make_memory(step))
            chain.append(step.id)
        elif isinstance(step, ToolStep):
            graph.add_node(f"check_{step.id}", make_check(step))
            graph.add_node(f"act_{step.id}", make_act(step))
            chain += [f"check_{step.id}", f"act_{step.id}"]
        elif isinstance(step, PluginStep):
            graph.add_node(step.id, make_plugin(step))
            chain.append(step.id)
        else:
            graph.add_node(f"prepare_{step.id}", make_prepare(step))
            graph.add_node(f"gate_{step.id}", make_gate(step))
            chain += [f"prepare_{step.id}", f"gate_{step.id}"]
    graph.add_node("finish", finish)
    chain.append("finish")
    graph.add_edge(START, "plan")
    for current, successor in zip(chain, chain[1:], strict=False):
        if successor != "finish" and current != "plan":
            # A rejection or failure skips every later step.
            graph.add_conditional_edges(current, _route(successor), [successor, "finish"])
        else:
            graph.add_edge(current, successor)
    graph.add_edge("finish", END)
    return graph


def _route(successor: str) -> Callable[[RunState], str]:
    def route(state: RunState) -> str:
        return "finish" if state.get("status") in STOP_STATES else successor

    return route
