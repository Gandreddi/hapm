"""Kafka consumer into oneview_events (deduped by event id) plus REST and SSE for the portal."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Annotated, Any

from aiokafka import AIOKafkaConsumer
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from hapm_core.auth import Principal, TokenVerifier, principal_dependency
from hapm_core.db import make_engine
from hapm_core.settings import CoreSettings
from hapm_events import Event
from hapm_events.envelope import ALL_TOPICS
from hapm_telemetry import configure_logging, configure_tracing, get_logger, health_router
from sqlalchemy import Engine, text

KIND = {
    "run.state_changed": "state",
    "run.step": "step",
    "run.tool_called": "tool",
    "run.memory_accessed": "memory",
    "run.policy_decided": "policy",
    "approval.requested": "approval",
    "approval.decided": "approval",
    "llm.usage": "llm",
    "eval.completed": "eval",
    "audit.recorded": "audit",
}


class OneViewSettings(CoreSettings):
    service_name: str = "oneview"
    kafka_bootstrap: str = "127.0.0.1:19092"
    consumer_group: str = "oneview"
    audit_ui_url: str = "http://127.0.0.1:8003/v1/audit"


def store_event(engine: Engine, raw: bytes) -> bool:
    """Insert one event; returns False when it was a duplicate (at-least-once delivery)."""
    event = Event.model_validate(json.loads(raw))
    with engine.begin() as conn:
        inserted = conn.execute(
            text(
                "INSERT INTO oneview_events (event_id, type, subject, time, data) VALUES (:i, :t, :s, :tm, CAST(:d AS jsonb)) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING seq"
            ),
            {"i": event.id, "t": event.type, "s": event.subject, "tm": event.time, "d": json.dumps(event.data)},
        ).first()
    return inserted is not None


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def create_app(settings: OneViewSettings | None = None) -> FastAPI:
    settings = settings or OneViewSettings()
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    log = get_logger()
    engine = make_engine(settings.database_url)
    verifier = TokenVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    viewer = principal_dependency(lambda: verifier, "viewer", "approver")
    status = {"consumer": False}

    async def consume() -> None:
        while True:
            consumer = AIOKafkaConsumer(
                *ALL_TOPICS,
                bootstrap_servers=settings.kafka_bootstrap,
                group_id=settings.consumer_group,
                auto_offset_reset="earliest",
                enable_auto_commit=False,
            )
            try:
                await consumer.start()
                status["consumer"] = True
                async for message in consumer:
                    try:
                        await asyncio.to_thread(store_event, engine, message.value)
                    except Exception:
                        log.exception("could not store event; skipping", offset=message.offset)
                    await consumer.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                status["consumer"] = False
                log.exception("consumer stopped; restarting")
                await asyncio.sleep(2)
            finally:
                with suppress(Exception):
                    await consumer.stop()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(consume())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="HAPM OneView-lite", version="0.1.0", lifespan=lifespan)

    async def consumer_ready() -> bool:
        return status["consumer"]

    app.include_router(health_router({"kafka_consumer": consumer_ready}))

    @app.get("/v1/runs")
    def runs(
        user: Annotated[Principal, Depends(viewer)], limit: int = Query(default=50, ge=1, le=500)
    ) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                WITH states AS (
                  SELECT DISTINCT ON (subject) subject, data->>'state' AS state, data->>'agent_id' AS agent_id, time
                  FROM oneview_events WHERE type = 'run.state_changed' ORDER BY subject, seq DESC
                ), first AS (
                  SELECT subject, min(time) AS started_at,
                         max(data->>'requester') FILTER (WHERE data->>'state' = 'queued') AS requester,
                         max(data->>'mode') FILTER (WHERE data->>'state' = 'queued') AS mode
                  FROM oneview_events WHERE type = 'run.state_changed' GROUP BY subject
                ), costs AS (
                  SELECT subject, sum((data->>'cost_usd')::numeric) AS cost_usd, count(*) AS llm_calls
                  FROM oneview_events WHERE type = 'llm.usage' GROUP BY subject
                )
                SELECT s.subject AS run_id, s.state, s.agent_id, s.time AS updated_at, f.started_at, f.requester, f.mode,
                       COALESCE(c.cost_usd, 0) AS cost_usd, COALESCE(c.llm_calls, 0) AS llm_calls
                FROM states s JOIN first f USING (subject) LEFT JOIN costs c USING (subject)
                ORDER BY f.started_at DESC LIMIT :l
            """),
                {"l": limit},
            ).all()
        return {"items": [{k: (float(v) if k == "cost_usd" else _iso(v)) for k, v in r._mapping.items()} for r in rows]}

    @app.get("/v1/runs/{run_id}/timeline")
    def timeline(run_id: str, user: Annotated[Principal, Depends(viewer)]) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT seq, event_id, type, time, data FROM oneview_events WHERE subject = :s ORDER BY seq"),
                {"s": run_id},
            ).all()
        if not rows:
            raise HTTPException(404, "no events for this run yet")
        items = [
            {
                "seq": r.seq,
                "event_id": r.event_id,
                "type": r.type,
                "kind": KIND.get(r.type, "other"),
                "time": _iso(r.time),
                "data": r.data,
            }
            for r in rows
        ]
        visible = [i for i in items if i["kind"] != "audit"]
        states = [i["data"]["state"] for i in items if i["type"] == "run.state_changed"]
        summary = {
            "state": states[-1] if states else None,
            "states": states,
            "cost_usd": round(sum(float(i["data"].get("cost_usd", 0)) for i in items if i["type"] == "llm.usage"), 6),
            "llm_calls": sum(1 for i in items if i["type"] == "llm.usage"),
            "tool_calls": sum(1 for i in items if i["type"] == "run.tool_called"),
            "memory_accesses": sum(1 for i in items if i["type"] == "run.memory_accessed"),
            "policy_decisions": sum(1 for i in items if i["type"] == "run.policy_decided"),
            "approvals": sum(1 for i in items if i["kind"] == "approval"),
            "audit_rows": sum(1 for i in items if i["type"] == "audit.recorded"),
            "steps": [i["data"]["step"] for i in items if i["type"] == "run.step"],
            "audit_url": f"{settings.audit_ui_url}?run_id={run_id}",
        }
        return {"run_id": run_id, "summary": summary, "items": visible}

    @app.get("/v1/evals")
    def evals(
        user: Annotated[Principal, Depends(viewer)], limit: int = Query(default=50, ge=1, le=500)
    ) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT time, data FROM oneview_events WHERE type = 'eval.completed' ORDER BY seq DESC LIMIT :l"),
                {"l": limit},
            ).all()
        return {"items": [{"time": _iso(r.time), **r.data} for r in rows]}

    @app.get("/v1/costs")
    def costs(user: Annotated[Principal, Depends(viewer)]) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT subject AS run_id, sum((data->>'cost_usd')::numeric) AS cost_usd, count(*) AS calls "
                    "FROM oneview_events WHERE type = 'llm.usage' AND subject IS NOT NULL GROUP BY subject "
                    "ORDER BY cost_usd DESC LIMIT 200"
                )
            ).all()
        return {"items": [{"run_id": r.run_id, "cost_usd": float(r.cost_usd), "calls": r.calls} for r in rows]}

    @app.get("/v1/stream")
    async def stream(
        request: Request,
        user: Annotated[Principal, Depends(viewer)],
        run_id: str | None = Query(default=None, max_length=128),
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        """Server-sent events: pushes every new event (optionally for one run) as it is consumed."""

        async def gen() -> AsyncIterator[str]:
            last = after
            if last == 0:
                with engine.connect() as conn:
                    last = int(conn.execute(text("SELECT COALESCE(max(seq), 0) FROM oneview_events")).scalar_one())
            yield ": connected\n\n"
            while not await request.is_disconnected():

                def fetch(after_seq: int) -> list[Any]:
                    with engine.connect() as conn:
                        return list(
                            conn.execute(
                                text(
                                    "SELECT seq, type, subject, time, data FROM oneview_events WHERE seq > :l "
                                    "AND (CAST(:r AS text) IS NULL OR subject = :r) AND type <> 'audit.recorded' ORDER BY seq LIMIT 200"
                                ),
                                {"l": after_seq, "r": run_id},
                            ).all()
                        )

                for r in await asyncio.to_thread(fetch, last):
                    last = r.seq
                    payload = {
                        "seq": r.seq,
                        "type": r.type,
                        "kind": KIND.get(r.type, "other"),
                        "subject": r.subject,
                        "time": _iso(r.time),
                        "data": r.data,
                    }
                    yield f"id: {r.seq}\nevent: hapm\ndata: {json.dumps(payload)}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app
