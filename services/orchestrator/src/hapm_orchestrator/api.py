"""Orchestrator API: tasks, runs, approvals and memory. Identity always comes from the bearer token."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from hapm_core.audit import AuditEntry
from hapm_core.auth import Principal, TokenVerifier, principal_dependency
from hapm_core.bootstrap import build_governor
from hapm_core.context import CallerContext
from hapm_core.ids import new_id
from hapm_memory import GatewayEmbedder, MemoryAccessDenied, MemoryScope, MemoryStore
from hapm_orchestrator import repo
from hapm_orchestrator.agents import AgentRegistry, config_hash
from hapm_orchestrator.autonomy import effective_autonomy
from hapm_orchestrator.settings import OrchestratorSettings
from hapm_telemetry import configure_logging, configure_tracing, health_router
from pydantic import BaseModel, Field
from sqlalchemy import text
from temporalio.client import Client
from temporalio.service import RPCError


class TaskRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    input: dict[str, Any]
    options: dict[str, float] = Field(default_factory=dict)
    mode: Literal["live", "eval"] = "live"


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    comment: str = Field(default="", max_length=2000)
    edited_args: dict[str, str] | None = None


class MemoryWrite(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    ttl_seconds: int | None = Field(default=None, gt=0)


class MemorySearch(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    k: int = Field(default=5, ge=1, le=50)


ALLOWED_OPTIONS = {"fake_delay_seconds", "approval_timeout_seconds"}


def _row(row: Any) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row._mapping.items()}


def create_app(settings: OrchestratorSettings | None = None) -> FastAPI:
    settings = settings or OrchestratorSettings(service_name="orchestrator-api")
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    gov = build_governor(settings)
    agents = AgentRegistry(settings.agents_dir)
    memory = MemoryStore(gov, GatewayEmbedder(settings.gateway_url))
    verifier = TokenVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    state: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state["temporal"] = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
        yield

    app = FastAPI(title="HAPM orchestrator", version="0.4.0", lifespan=lifespan)
    any_user = principal_dependency(lambda: verifier)
    approver = principal_dependency(lambda: verifier, "approver")
    admin = principal_dependency(lambda: verifier, "admin")

    async def db_ready() -> bool:
        with gov.engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    async def temporal_ready() -> bool:
        return "temporal" in state

    app.include_router(health_router({"postgres": db_ready, "temporal": temporal_ready}))

    @app.get("/v1/agents")
    def list_agents(user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        out = []
        with gov.engine.connect() as conn:
            for agent in agents.all():
                autonomy, reason = effective_autonomy(conn, agent)
                out.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "requested_autonomy": agent.autonomy.value,
                        "effective_autonomy": autonomy.value,
                        "reason": reason,
                        "config_hash": config_hash(agent),
                    }
                )
        return {"items": out}

    @app.post("/v1/tasks", status_code=201)
    async def submit_task(body: TaskRequest, user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        if body.mode == "eval" and not user.has_any("admin"):
            raise HTTPException(403, "eval mode requires the admin role")
        unknown = set(body.options) - ALLOWED_OPTIONS
        if unknown:
            raise HTTPException(422, f"unknown options {sorted(unknown)}")
        if len(json.dumps(body.input)) > 50_000:
            raise HTTPException(422, "input too large")
        try:
            agent = agents.load(body.agent_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown agent {body.agent_id}") from exc
        with gov.engine.connect() as conn:
            autonomy, reason = effective_autonomy(conn, agent)
        task_id, run_id = new_id("task"), new_id("run")
        repo.create_task_and_run(
            gov,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent.id,
            requester=user.sub,
            task_input={**body.input, "options": body.options},
            mode=body.mode,
            config_hash=config_hash(agent),
            autonomy=autonomy.value,
        )
        await state["temporal"].start_workflow("agent_run", run_id, id=run_id, task_queue=settings.task_queue)
        return {"task_id": task_id, "run_id": run_id, "autonomy": autonomy.value, "autonomy_reason": reason}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            task = repo.get_task(conn, task_id)
            if task is None:
                raise HTTPException(404, "task not found")
            runs = conn.execute(text("SELECT id, state FROM runs WHERE task_id = :t"), {"t": task_id}).all()
        return {**_row(task), "runs": [_row(r) for r in runs]}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            run = repo.get_run(conn, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            approvals = conn.execute(
                text("SELECT * FROM approvals WHERE run_id = :r ORDER BY created_at"), {"r": run_id}
            ).all()
            cost = conn.execute(
                text("SELECT COALESCE(sum(cost_usd),0) FROM llm_usage WHERE run_id = :r"), {"r": run_id}
            ).scalar()
        return {**_row(run), "approvals": [_row(a) for a in approvals], "llm_cost_usd": float(cost)}

    @app.post("/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            run = repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.requester != user.sub and not user.has_any("admin"):
            raise HTTPException(403, "only the requester or an admin can cancel a run")
        if run.state in repo.TERMINAL:
            raise HTTPException(409, f"run already {run.state}")
        gov.record_outcome(
            AuditEntry(
                actor=f"user:{user.sub}",
                action="run.cancel",
                resource=run_id,
                decision="requested",
                run_id=run_id,
                task_id=run.task_id,
            )
        )
        await state["temporal"].get_workflow_handle(run_id).cancel()
        return {"run_id": run_id, "status": "cancel_requested"}

    @app.get("/v1/approvals")
    def list_approvals(
        user: Annotated[Principal, Depends(any_user)],
        status: Annotated[str | None, Query(pattern="^(pending|approved|rejected|expired|void)$")] = "pending",
    ) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.*, r.agent_id, r.requester FROM approvals a JOIN runs r ON r.id = a.run_id "
                    "WHERE (CAST(:s AS text) IS NULL OR a.status = :s) ORDER BY a.created_at DESC LIMIT 200"
                ),
                {"s": status},
            ).all()
        return {"items": [_row(r) for r in rows]}

    @app.get("/v1/approvals/{approval_id}")
    def get_approval(approval_id: str, user: Annotated[Principal, Depends(any_user)]) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.*, r.agent_id, r.requester FROM approvals a JOIN runs r ON r.id = a.run_id WHERE a.id = :i"
                ),
                {"i": approval_id},
            ).first()
        if row is None:
            raise HTTPException(404, "approval not found")
        return _row(row)

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(
        approval_id: str, body: DecisionRequest, user: Annotated[Principal, Depends(approver)]
    ) -> dict[str, Any]:
        with gov.engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT a.*, r.requester FROM approvals a JOIN runs r ON r.id = a.run_id WHERE a.id = :i FOR UPDATE OF a"
                ),
                {"i": approval_id},
            ).first()
            if row is None:
                raise HTTPException(404, "approval not found")
            policy = gov.policy.approval_policy(row.approval_policy_id)

            def refuse(status: int, reason: str, rule: str) -> HTTPException:
                # Separate transaction, so the refusal stays audited when this one rolls back.
                gov.record_outcome(
                    AuditEntry(
                        actor=f"user:{user.sub}",
                        action="approval.decide",
                        resource=approval_id,
                        decision="deny",
                        run_id=row.run_id,
                        task_id=row.task_id,
                        policy_rule_id=rule,
                        inputs={"reason": reason},
                    )
                )
                return HTTPException(status, reason)

            if row.status != "pending":
                raise refuse(409, f"approval is already {row.status}", "approval-not-pending")
            if row.expires_at <= datetime.now(UTC):
                raise refuse(409, "approval has expired", "approval-expired")
            approvers = list(row.approvers) if row.approvers else policy.approvers
            if not (user.sub in approvers or any(r in approvers for r in user.roles)):
                raise refuse(
                    403, "you are not on the approver list for this policy", f"{row.approval_policy_id}.approvers"
                )
            if policy.forbid_self_approval and user.sub == row.requester:
                raise refuse(
                    403, "policy forbids approving your own task", f"{row.approval_policy_id}.forbid_self_approval"
                )
            edited = None
            if body.decision == "edit":
                if not body.edited_args or not set(body.edited_args) <= set(row.proposed_action["args"]):
                    raise refuse(422, "edited_args must change only existing arguments", "approval-edit-invalid")
                edited = body.edited_args
            final = "reject" if body.decision == "reject" else "approve"
            stored = {"approve": "approved", "edit": "approved", "reject": "rejected"}[body.decision]
            conn.execute(
                text(
                    "UPDATE approvals SET status = :st, decision = :d, decided_by = :u, comment = :c, "
                    "edited_args = CAST(:e AS jsonb), decided_at = now() WHERE id = :i"
                ),
                {
                    "st": stored,
                    "d": body.decision,
                    "u": user.sub,
                    "c": body.comment,
                    "e": json.dumps(edited) if edited else None,
                    "i": approval_id,
                },
            )
            gov.audit(
                conn,
                AuditEntry(
                    actor=f"user:{user.sub}",
                    action="approval.decide",
                    resource=approval_id,
                    decision=body.decision,
                    run_id=row.run_id,
                    task_id=row.task_id,
                    policy_rule_id=row.approval_policy_id,
                    inputs={"comment": body.comment, "edited_args": edited},
                ),
            )
            gov.emit(
                conn,
                "approval.decided",
                {
                    "approval_id": approval_id,
                    "run_id": row.run_id,
                    "decision": body.decision,
                    "decided_by": user.sub,
                    "comment": body.comment,
                },
                subject=row.run_id,
            )
            try:
                # Signal before commit: if the run is no longer waiting, the decision is rolled back.
                await (
                    state["temporal"]
                    .get_workflow_handle(row.run_id)
                    .signal("decide", {"decision": final, "edited_args": edited, "decided_by": user.sub})
                )
            except RPCError as exc:
                raise refuse(409, f"run is no longer waiting for approval: {exc.message}", "run-not-waiting") from exc
        return {"approval_id": approval_id, "decision": body.decision, "decided_by": user.sub}

    def agent_ctx(agent_id: str | None, run_id: str | None, task_id: str | None) -> tuple[CallerContext, list[str]]:
        if not agent_id:
            raise HTTPException(400, "X-HAPM-Agent-Id header is required")
        try:
            agent = agents.load(agent_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown agent {agent_id}") from exc
        return CallerContext(agent_id=agent.id, team_id=agent.team_id, run_id=run_id, task_id=task_id), list(
            agent.memory_scopes
        )

    @app.post("/v1/memory/{scope}", status_code=201)
    def memory_write(
        scope: MemoryScope,
        body: MemoryWrite,
        user: Annotated[Principal, Depends(admin)],
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        ctx, allowed = agent_ctx(x_hapm_agent_id, x_hapm_run_id, x_hapm_task_id)
        try:
            return {"id": memory.write(ctx, scope, body.content, allowed, ttl_seconds=body.ttl_seconds)}
        except MemoryAccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/memory/{scope}")
    def memory_read(
        scope: MemoryScope,
        user: Annotated[Principal, Depends(admin)],
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        ctx, allowed = agent_ctx(x_hapm_agent_id, x_hapm_run_id, x_hapm_task_id)
        try:
            records = memory.read(ctx, scope, allowed)
        except MemoryAccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"items": [{"id": r.id, "content": r.content, "created_at": r.created_at.isoformat()} for r in records]}

    @app.post("/v1/memory/{scope}/search")
    def memory_search(
        scope: MemoryScope,
        body: MemorySearch,
        user: Annotated[Principal, Depends(admin)],
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        ctx, allowed = agent_ctx(x_hapm_agent_id, x_hapm_run_id, x_hapm_task_id)
        try:
            records = memory.search(ctx, scope, body.query, allowed, k=body.k)
        except MemoryAccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"items": [{"id": r.id, "content": r.content, "distance": r.distance} for r in records]}

    @app.delete("/v1/memory/{scope}/{record_id}")
    def memory_delete(
        scope: MemoryScope,
        record_id: str,
        user: Annotated[Principal, Depends(admin)],
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        ctx, allowed = agent_ctx(x_hapm_agent_id, x_hapm_run_id, x_hapm_task_id)
        try:
            return {"deleted": memory.delete(ctx, scope, record_id, allowed)}
        except MemoryAccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc

    return app
