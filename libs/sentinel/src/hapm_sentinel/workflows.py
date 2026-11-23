"""Fleet scheduler as Temporal workflows (registered by the orchestrator worker).

sentinel_fleet starts one sentinel_repo child workflow per repository, at most `concurrency` at a time,
then runs the link activity. Each repository build is an activity with retries and backoff; a build that
already stored its SHA is skipped on retry ("unchanged"), so a killed worker resumes without duplicate work.
"""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

TASK_QUEUE = "hapm-agent-runs"
BUILD_OPTIONS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(minutes=60),
    "retry_policy": RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
        maximum_attempts=4,
    ),
}


def _context(runner: str | None, source: str) -> Any:
    from hapm_config import load_project
    from hapm_core.db import make_engine
    from hapm_sentinel.build import BuildContext
    from hapm_sentinel.cli import database_url
    from hapm_sentinel.engines.runner import runner_for
    from hapm_sentinel.ontology import ontology
    from hapm_sentinel.sources import reader_for
    from hapm_sentinel.store import SentinelStore

    project = load_project()
    return BuildContext(
        project,
        SentinelStore(make_engine(database_url())),
        reader_for(source),
        runner_for(project.sentinel.engines, runner),
        ontology(),
    )


@activity.defn(name="sentinel_build_repo")
def build_repo_activity(params: dict[str, Any]) -> dict[str, Any]:
    from hapm_sentinel.build import build_repo

    ctx = _context(params.get("runner"), params.get("source", "connector"))
    result = build_repo(
        ctx,
        params["repo"],
        force=bool(params.get("force")),
        fleet_run_id=params.get("fleet_run_id"),
        run_id=params.get("run_id"),
    )
    if result["status"] == "failed":
        raise RuntimeError(f"{params['repo']}: {result.get('error')}")
    return {k: result.get(k) for k in ("repo", "run_id", "sha", "status", "precision_tier", "nodes", "edges")}


@activity.defn(name="sentinel_link")
def link_activity(params: dict[str, Any]) -> dict[str, Any]:
    from hapm_sentinel.build import link_fleet

    report = link_fleet(_context(params.get("runner"), params.get("source", "connector")))
    return {"edges": report["edges"], "confidence": report["confidence"]}


@activity.defn(name="sentinel_plan")
def plan_activity(params: dict[str, Any]) -> list[str]:
    from hapm_sentinel.build import enabled_repos

    ctx = _context(params.get("runner"), params.get("source", "connector"))
    return enabled_repos(ctx.project, params.get("repos"))


@workflow.defn(name="sentinel_repo")
class SentinelRepoWorkflow:
    @workflow.run
    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await workflow.execute_activity("sentinel_build_repo", params, **BUILD_OPTIONS)
            return result
        except Exception as exc:  # one repository failing never fails the fleet
            return {"repo": params["repo"], "status": "failed", "error": str(exc)[:300]}


@workflow.defn(name="sentinel_fleet")
class SentinelFleetWorkflow:
    @workflow.run
    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        short: dict[str, Any] = {
            "start_to_close_timeout": timedelta(minutes=5),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        }
        repos: list[str] = await workflow.execute_activity("sentinel_plan", params, **short)
        concurrency = max(1, int(params.get("concurrency", 4)))
        fleet_id = workflow.info().workflow_id
        results: list[dict[str, Any]] = []
        for i in range(0, len(repos), concurrency):
            batch = repos[i : i + concurrency]
            handles = [
                workflow.execute_child_workflow(
                    "sentinel_repo",
                    {**params, "repo": name, "fleet_run_id": fleet_id, "run_id": f"{fleet_id}-{name}"},
                    id=f"{fleet_id}-{name}",
                )
                for name in batch
            ]
            results.extend(await asyncio.gather(*handles))
        links = await workflow.execute_activity("sentinel_link", params, **BUILD_OPTIONS) if repos else {}
        return {"fleet_run_id": fleet_id, "repos": results, "links": links}


WORKFLOWS = [SentinelFleetWorkflow, SentinelRepoWorkflow]
ACTIVITIES = [build_repo_activity, link_activity, plan_activity]


def start_fleet(
    repos: list[str] | None,
    force: bool,
    runner: str | None,
    source: str,
    address: str = "127.0.0.1:7233",
    wait: bool = True,
) -> dict[str, Any]:
    """Start sentinel_fleet on the orchestrator's task queue (and wait for the result by default)."""
    import uuid

    from hapm_config import load_project
    from temporalio.client import Client

    async def go() -> dict[str, Any]:
        client = await Client.connect(address)
        workflow_id = f"sentinel-fleet-{uuid.uuid4().hex[:12]}"
        params = {
            "repos": repos,
            "force": force,
            "runner": runner,
            "source": source,
            "concurrency": load_project().sentinel.concurrency,
        }
        handle = await client.start_workflow("sentinel_fleet", params, id=workflow_id, task_queue=TASK_QUEUE)
        if not wait:
            return {"workflow_id": workflow_id}
        result: dict[str, Any] = await handle.result()
        return result

    return asyncio.run(go())
