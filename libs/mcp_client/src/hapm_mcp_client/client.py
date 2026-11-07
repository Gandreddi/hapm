"""Call MCP tools. Order: allow-list, size limit, policy, (approval or dry-run), schema, call, audit."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hapm_core.audit import AuditEntry
from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_policy import Effect
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from hapm_mcp_client.registry import ToolRegistry, ToolSpec

ALLOW_LIST_RULE = "agent-tool-allow-list"


class ToolCallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ToolResult:
    status: str  # ok | approval_required | dry_run
    tool: str
    kind: str
    output: Any = None
    rule_id: str | None = None
    approval_policy_id: str | None = None
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


Transport = Callable[[str, str, dict[str, Any], float], Awaitable[tuple[dict[str, Any] | None, Any]]]


async def mcp_transport(url: str, tool: str, args: dict[str, Any], timeout: float) -> tuple[dict[str, Any] | None, Any]:
    """Open an MCP session, find the tool's input schema, call it and return (schema, output).

    Errors are raised only after the session closes, because the SDK's task group would
    otherwise wrap them in an ExceptionGroup and hide them from the caller.
    """
    error: ToolCallError | None = None
    schema: dict[str, Any] | None = None
    output: Any = None
    async with streamablehttp_client(url, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            schema = next((t.inputSchema for t in listed.tools if t.name == tool), None)
            if schema is None:
                error = ToolCallError("unknown_tool", f"server does not offer {tool}")
            else:
                error = _validate(schema, args, tool)
            if error is None:
                result = await session.call_tool(tool, args)
                if result.isError:
                    text = " ".join(getattr(c, "text", "") for c in result.content)
                    error = ToolCallError("tool_error", text or "tool returned an error")
                elif result.structuredContent is not None:
                    output = result.structuredContent
                else:
                    joined = "".join(getattr(c, "text", "") for c in result.content)
                    try:
                        output = json.loads(joined)
                    except json.JSONDecodeError:
                        output = joined
    if error is not None:
        raise error
    return schema, output


def _validate(schema: dict[str, Any], args: dict[str, Any], tool: str) -> ToolCallError | None:
    errors = list(Draft202012Validator(schema).iter_errors(args))
    if errors:
        return ToolCallError("invalid_arguments", f"{tool}: {errors[0].message}")
    return None


class ToolClient:
    def __init__(self, registry: ToolRegistry, governor: Governor, transport: Transport = mcp_transport) -> None:
        self.registry = registry
        self.governor = governor
        self.transport = transport

    def _audit_deny(self, ctx: CallerContext, tool: str, rule: str, reason: str) -> None:
        self.governor.record_outcome(
            AuditEntry(
                actor=ctx.actor,
                action="tool.call",
                resource=tool,
                decision="deny",
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                policy_rule_id=rule,
                inputs={"reason": reason},
            )
        )

    def _event(self, ctx: CallerContext, spec: ToolSpec, status: str) -> None:
        with self.governor.engine.begin() as conn:
            self.governor.emit(
                conn,
                "run.tool_called",
                {
                    "run_id": ctx.run_id or "",
                    "task_id": ctx.task_id or "",
                    "tool": spec.name,
                    "kind": spec.kind,
                    "status": status,
                },
                subject=ctx.run_id,
            )

    async def call(
        self,
        ctx: CallerContext,
        tool: str,
        args: dict[str, Any],
        allowed_tools: list[str],
        *,
        approved: bool = False,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        spec = self.registry.get(tool)
        if spec is None or tool not in allowed_tools:
            await asyncio.to_thread(self._audit_deny, ctx, tool, ALLOW_LIST_RULE, "tool not on agent allow-list")
            raise ToolCallError("not_allowed", f"agent {ctx.agent_id} may not use tool {tool}")
        if len(json.dumps(args).encode()) > self.registry.limit(spec, "max_args_bytes"):
            await asyncio.to_thread(self._audit_deny, ctx, tool, "tool-size-limit", "arguments too large")
            raise ToolCallError("too_large", f"arguments for {tool} exceed the size limit")

        decision = await asyncio.to_thread(
            self.governor.decide,
            ctx.actor,
            "tool.call",
            tool,
            attributes={"kind": spec.kind, "mode": ctx.mode},
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            inputs=args,
        )
        if decision.effect is Effect.DENY:
            raise ToolCallError("policy_denied", f"policy {decision.rule_id} denied {tool}")
        if decision.effect is Effect.REQUIRE_APPROVAL and not approved:
            await asyncio.to_thread(self._event, ctx, spec, "approval_required")
            return ToolResult(
                "approval_required",
                tool,
                spec.kind,
                rule_id=decision.rule_id,
                approval_policy_id=decision.approval_policy_id,
            )
        if spec.kind == "write" and ctx.mode == "eval":
            await asyncio.to_thread(self._finish, ctx, spec, args, {"dry_run": True}, "dry_run")
            return ToolResult(
                "dry_run", tool, spec.kind, output={"dry_run": True, "args": args}, rule_id=decision.rule_id
            )

        call_args = dict(args)
        if spec.kind == "write":
            if not idempotency_key:
                raise ToolCallError("idempotency_key_required", f"write tool {tool} needs an idempotency key")
            call_args["idempotency_key"] = idempotency_key
        url = self.registry.servers[spec.server].url
        timeout = self.registry.limit(spec, "timeout_seconds")
        retries = int(self.registry.limit(spec, "retries"))
        attempt = 0
        while True:
            attempt += 1
            try:
                _, output = await asyncio.wait_for(self.transport(url, tool, call_args, timeout), timeout + 5)
                break
            except ToolCallError:
                raise
            except Exception as exc:
                if attempt > retries:
                    await asyncio.to_thread(self._finish, ctx, spec, call_args, {"error": str(exc)}, "error")
                    raise ToolCallError("unavailable", f"{tool} failed after {attempt} attempts: {exc}") from exc
                await asyncio.sleep(min(2 ** (attempt - 1) * 0.5, 4))
        if len(json.dumps(output, default=str).encode()) > self.registry.limit(spec, "max_response_bytes"):
            await asyncio.to_thread(self._finish, ctx, spec, call_args, {"error": "response too large"}, "error")
            raise ToolCallError("too_large", f"response from {tool} exceeds the size limit")
        await asyncio.to_thread(self._finish, ctx, spec, call_args, output, "ok")
        return ToolResult("ok", tool, spec.kind, output=output, rule_id=decision.rule_id, attempts=attempt)

    def _finish(self, ctx: CallerContext, spec: ToolSpec, args: Any, output: Any, status: str) -> None:
        with self.governor.engine.begin() as conn:
            self.governor.audit(
                conn,
                AuditEntry(
                    actor=ctx.actor,
                    action="tool.call.completed",
                    resource=spec.name,
                    decision=status,
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    inputs=args,
                    outputs=output,
                ),
            )
            self.governor.emit(
                conn,
                "run.tool_called",
                {
                    "run_id": ctx.run_id or "",
                    "task_id": ctx.task_id or "",
                    "tool": spec.name,
                    "kind": spec.kind,
                    "status": status,
                },
                subject=ctx.run_id,
            )
