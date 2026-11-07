"""LLM gateway: POST /v1/chat and POST /v1/embeddings (OpenAI-style shapes)."""

import asyncio
import time
import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from hapm_core.context import CallerContext
from hapm_policy import Effect
from hapm_telemetry import ReadinessCheck, configure_logging, configure_tracing, get_logger, health_router
from pydantic import BaseModel, Field

from hapm_gateway.budgets import BudgetExceeded, BudgetGuard, Counter, MemoryCounter, RedisCounter
from hapm_gateway.config import GatewayConfig
from hapm_gateway.providers import ChatResult, EmbedResult, Provider, ProviderError, build_provider
from hapm_gateway.recorder import Recorder, Usage
from hapm_gateway.redaction import Redactor
from hapm_gateway.router import Router
from hapm_gateway.settings import GatewaySettings


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class ChatRequest(BaseModel):
    model: str = Field(min_length=1, max_length=64)
    messages: list[Message] = Field(min_length=1, max_length=200)
    max_tokens: int | None = Field(default=None, gt=0, le=32_000)


class EmbeddingsRequest(BaseModel):
    model: str = Field(default="embed", min_length=1, max_length=64)
    input: str | list[str]


def tcp_check(host: str, port: int, timeout: float) -> ReadinessCheck:
    """Readiness check that passes when the host accepts TCP connections."""

    async def check() -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, TimeoutError):
            return False
        writer.close()
        await writer.wait_closed()
        return True

    return check


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(
    settings: GatewaySettings | None = None,
    *,
    config: GatewayConfig | None = None,
    recorder: Recorder | None = None,
    counter: Counter | None = None,
    providers: dict[str, Provider] | None = None,
) -> FastAPI:
    settings = settings or GatewaySettings()
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    log = get_logger()
    config = config or GatewayConfig.load(settings.config_path)
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))
    if providers is None:
        built = {n: build_provider(n, c, client) for n, c in config.providers.items()}
        providers = {n: p for n, p in built.items() if p is not None}
    if counter is None:
        counter = RedisCounter(settings.redis_url) if settings.redis_url else MemoryCounter()
    if recorder is None:
        from hapm_core.bootstrap import build_governor

        from hapm_gateway.recorder import GovernorRecorder

        recorder = GovernorRecorder(build_governor(settings))
    router = Router(config, providers)
    budgets = BudgetGuard(counter)
    redact = Redactor(config.redaction)
    active_recorder: Recorder = recorder

    app = FastAPI(title="HAPM LLM gateway", version="0.2.0")
    checks: dict[str, ReadinessCheck] = {
        "postgres": tcp_check(settings.db_host, settings.db_port, settings.ready_timeout_seconds)
    }
    if isinstance(counter, RedisCounter):
        checks["redis"] = counter.ping
    app.include_router(health_router(checks))

    def caller(agent: str | None, team: str | None, run: str | None, task: str | None) -> CallerContext:
        if not agent or not team:
            raise HTTPException(400, "X-HAPM-Agent-Id and X-HAPM-Team-Id headers are required")
        try:
            return CallerContext(agent_id=agent, team_id=team, run_id=run, task_id=task)
        except ValueError as exc:
            raise HTTPException(400, f"invalid caller identity: {exc}") from exc

    async def guarded(ctx: CallerContext, action: str, alias: str) -> JSONResponse | None:
        decision = await asyncio.to_thread(active_recorder.decide, ctx, action, alias)
        if decision.effect is not Effect.ALLOW:
            return _error(403, "policy_denied", f"policy {decision.rule_id} returned {decision.effect.value}")
        try:
            await budgets.check(config.budgets.for_team(ctx.team_id), ctx.team_id, ctx.run_id)
        except BudgetExceeded as exc:
            return _error(429, "budget_exceeded", exc.reason())
        return None

    async def finish(ctx: CallerContext, action: str, usage: Usage) -> None:
        await budgets.charge(ctx.team_id, ctx.run_id, usage.prompt_tokens + usage.completion_tokens, usage.cost_usd)
        await asyncio.to_thread(active_recorder.record, ctx, action, usage)

    def cost(model: str, prompt: int, completion: int) -> float:
        price = config.price(model)
        return round(prompt / 1000 * price.prompt + completion / 1000 * price.completion, 6)

    @app.post("/v1/chat", response_model=None)
    async def chat(
        body: ChatRequest,
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_team_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
        x_hapm_fake_delay: Annotated[float | None, Header(ge=0, le=60)] = None,
    ) -> dict[str, Any] | JSONResponse:
        ctx = caller(x_hapm_agent_id, x_hapm_team_id, x_hapm_run_id, x_hapm_task_id)
        refused = await guarded(ctx, "llm.chat", body.model)
        if refused:
            return refused
        messages = [m.model_dump() for m in body.messages]
        options = {"fake_delay_seconds": x_hapm_fake_delay or 0}
        try:
            routed = await router.call(body.model, lambda p, m: p.chat(m, messages, body.max_tokens, options))
        except ProviderError as exc:
            log.warning("chat failed", alias=body.model, status=exc.status, error=str(exc), run_id=ctx.run_id)
            return _error(exc.status, "provider_error", str(exc))
        result: ChatResult = routed.result
        usage = Usage(
            alias=body.model,
            provider=routed.provider,
            model=routed.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=cost(routed.model, result.prompt_tokens, result.completion_tokens),
            latency_ms=round(routed.latency_ms, 2),
            status="ok",
            request={"messages": [{"role": m["role"], "content": redact(m["content"])} for m in messages]},
            response={"content": redact(result.content), "attempts": routed.attempts},
        )
        await finish(ctx, "llm.chat", usage)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.prompt_tokens + result.completion_tokens,
            },
            "hapm": {
                "provider": routed.provider,
                "provider_model": routed.model,
                "cost_usd": usage.cost_usd,
                "attempts": routed.attempts,
            },
        }

    @app.post("/v1/embeddings", response_model=None)
    async def embeddings(
        body: EmbeddingsRequest,
        x_hapm_agent_id: Annotated[str | None, Header()] = None,
        x_hapm_team_id: Annotated[str | None, Header()] = None,
        x_hapm_run_id: Annotated[str | None, Header()] = None,
        x_hapm_task_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any] | JSONResponse:
        ctx = caller(x_hapm_agent_id, x_hapm_team_id, x_hapm_run_id, x_hapm_task_id)
        inputs = [body.input] if isinstance(body.input, str) else body.input
        if not inputs or len(inputs) > 256 or any(len(i) > 100_000 for i in inputs):
            raise HTTPException(422, "input must hold 1-256 strings of at most 100000 characters")
        refused = await guarded(ctx, "llm.embed", body.model)
        if refused:
            return refused
        try:
            routed = await router.call(body.model, lambda p, m: p.embed(m, inputs))
        except ProviderError as exc:
            return _error(exc.status, "provider_error", str(exc))
        result: EmbedResult = routed.result
        usage = Usage(
            alias=body.model,
            provider=routed.provider,
            model=routed.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=0,
            cost_usd=cost(routed.model, result.prompt_tokens, 0),
            latency_ms=round(routed.latency_ms, 2),
            status="ok",
            request={"inputs": [redact(i) for i in inputs]},
            response={"count": len(result.vectors)},
        )
        await finish(ctx, "llm.embed", usage)
        return {
            "object": "list",
            "model": body.model,
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(result.vectors)],
            "usage": {"prompt_tokens": result.prompt_tokens, "total_tokens": result.prompt_tokens},
        }

    return app
