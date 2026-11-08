"""POST /v1/evals/run runs a suite through the orchestrator in eval mode and stores the scores."""

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from hapm_core.auth import Principal, TokenVerifier, principal_dependency
from hapm_core.bootstrap import build_governor
from hapm_core.context import CallerContext
from hapm_core.ids import new_id
from hapm_core.scoring import score_output
from hapm_core.settings import CoreSettings
from hapm_telemetry import configure_logging, configure_tracing, get_logger, health_router
from pydantic import BaseModel, Field
from sqlalchemy import text

from hapm_eval.suite import Case, Suite, load_suite

JUDGE_SYSTEM = 'You are an evaluation judge. Reply only with JSON: {"score": <0..1>, "reason": "..."}.'
TERMINAL = {"succeeded", "failed", "rejected", "cancelled"}


class EvalSettings(CoreSettings):
    service_name: str = "eval"
    orchestrator_url: str = "http://127.0.0.1:8002"
    gateway_url: str = "http://127.0.0.1:8001"
    oidc_token_url: str = "http://127.0.0.1:8006/token"  # noqa: S105 - a URL, not a secret
    eval_client_id: str = "eval-service"
    eval_client_secret: str = "eval-service-dev"  # noqa: S105 - dev stub value; Key Vault reference in cloud
    case_timeout_seconds: float = 300

    @property
    def suites_dir(self) -> Path:
        return self.agents_dir / "evals"


class RunRequest(BaseModel):
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(default="1", max_length=32)


def create_app(settings: EvalSettings | None = None) -> FastAPI:
    settings = settings or EvalSettings()
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    log = get_logger()
    gov = build_governor(settings)
    app = FastAPI(title="HAPM eval", version="0.1.0")
    verifier = TokenVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    admin = principal_dependency(lambda: verifier, "admin")
    viewer = principal_dependency(lambda: verifier, "viewer")

    async def db_ready() -> bool:
        with gov.engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    app.include_router(health_router({"postgres": db_ready}))

    async def service_token(client: httpx.AsyncClient) -> str:
        resp = await client.post(
            settings.oidc_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.eval_client_id,
                "client_secret": settings.eval_client_secret,
            },
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])

    async def judge(client: httpx.AsyncClient, suite: Suite, output: dict[str, Any]) -> float:
        assert suite.judge is not None
        ctx = CallerContext(agent_id="eval_judge", team_id="eval")
        body = {
            "model": suite.judge.alias,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": f"Rubric: {suite.judge.rubric}\nOutput: {output.get('summary')}"},
            ],
        }
        resp = await client.post(f"{settings.gateway_url}/v1/chat", json=body, headers=ctx.headers())
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        return float(json.loads(match.group(0))["score"]) if match else 0.0

    async def run_case(client: httpx.AsyncClient, headers: dict[str, str], suite: Suite, case: Case) -> dict[str, Any]:
        resp = await client.post(
            f"{settings.orchestrator_url}/v1/tasks",
            headers=headers,
            json={"agent_id": suite.agent_id, "input": case.input, "mode": "eval"},
        )
        resp.raise_for_status()
        run_id = resp.json()["run_id"]
        deadline = time.monotonic() + settings.case_timeout_seconds
        while True:
            run = (await client.get(f"{settings.orchestrator_url}/v1/runs/{run_id}", headers=headers)).json()
            if run["state"] in TERMINAL:
                break
            if time.monotonic() > deadline:
                return {"id": case.id, "run_id": run_id, "score": 0.0, "checks": [], "error": "timed out"}
            await asyncio.sleep(0.5)
        if run["state"] != "succeeded":
            return {
                "id": case.id,
                "run_id": run_id,
                "score": 0.0,
                "checks": [],
                "error": f"run {run['state']}: {run.get('error')}",
            }
        output = run["output"] or {}
        judge_score = await judge(client, suite, output) if case.judge and suite.judge else None
        return {"id": case.id, "run_id": run_id, **score_output(case.expect, output, judge_score)}

    @app.post("/v1/evals/run")
    async def run_eval(body: RunRequest, user: Annotated[Principal, Depends(admin)]) -> dict[str, Any]:
        try:
            suite = load_suite(settings.suites_dir, body.suite_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown suite {body.suite_id}") from exc
        if suite.agent_id != body.agent_id:
            raise HTTPException(422, f"suite {suite.id} is for agent {suite.agent_id}")
        async with httpx.AsyncClient(timeout=60) as client:
            headers = {"Authorization": f"Bearer {await service_token(client)}"}
            agents = (await client.get(f"{settings.orchestrator_url}/v1/agents", headers=headers)).json()["items"]
            agent = next((a for a in agents if a["id"] == body.agent_id), None)
            if agent is None:
                raise HTTPException(404, f"unknown agent {body.agent_id}")
            cases = await asyncio.gather(*(run_case(client, headers, suite, c) for c in suite.cases))
        aggregate = round(sum(c["score"] for c in cases) / len(cases), 4)
        passed = aggregate >= suite.threshold
        eval_id = new_id("eval")
        with gov.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO eval_results (id, kind, agent_id, suite_id, version, config_hash, aggregate, threshold, passed, cases) "
                    "VALUES (:i, 'offline', :a, :s, :v, :h, :agg, :t, :p, CAST(:c AS jsonb))"
                ),
                {
                    "i": eval_id,
                    "a": body.agent_id,
                    "s": suite.id,
                    "v": body.version,
                    "h": agent["config_hash"],
                    "agg": aggregate,
                    "t": suite.threshold,
                    "p": passed,
                    "c": json.dumps(cases),
                },
            )
            gov.emit(
                conn,
                "eval.completed",
                {
                    "eval_id": eval_id,
                    "agent_id": body.agent_id,
                    "suite_id": suite.id,
                    "aggregate": aggregate,
                    "passed": passed,
                    "kind": "offline",
                    "threshold": suite.threshold,
                },
                subject=f"eval:{body.agent_id}",
            )
        log.info("eval finished", eval_id=eval_id, aggregate=aggregate, passed=passed)
        return {
            "eval_id": eval_id,
            "agent_id": body.agent_id,
            "suite_id": suite.id,
            "version": body.version,
            "config_hash": agent["config_hash"],
            "aggregate": aggregate,
            "threshold": suite.threshold,
            "passed": passed,
            "cases": cases,
        }

    @app.get("/v1/evals")
    def list_evals(
        user: Annotated[Principal, Depends(viewer)],
        agent_id: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        with gov.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, kind, agent_id, suite_id, version, config_hash, run_id, aggregate, threshold, passed, created_at "
                    "FROM eval_results WHERE (CAST(:a AS text) IS NULL OR agent_id = :a) ORDER BY created_at DESC LIMIT :l"
                ),
                {"a": agent_id, "l": limit},
            ).all()
        return {
            "items": [
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._mapping.items()} for r in rows
            ]
        }

    return app
