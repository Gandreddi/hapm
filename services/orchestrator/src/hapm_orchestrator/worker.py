"""Temporal worker process: python -m hapm_orchestrator.worker"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from hapm_core.bootstrap import build_governor
from hapm_mcp_client import ToolClient, ToolRegistry
from hapm_memory import GatewayEmbedder, MemoryStore
from hapm_orchestrator.activities import Activities
from hapm_orchestrator.agents import AgentRegistry
from hapm_orchestrator.graph import Deps
from hapm_orchestrator.online_eval import make_online_scorer
from hapm_orchestrator.settings import OrchestratorSettings
from hapm_orchestrator.workflow import AgentRunWorkflow
from hapm_telemetry import configure_logging, configure_tracing, get_logger
from temporalio.client import Client
from temporalio.worker import Worker


def build_activities(settings: OrchestratorSettings) -> Activities:
    governor = build_governor(settings)
    deps = Deps(
        governor=governor,
        memory=MemoryStore(governor, GatewayEmbedder(settings.gateway_url)),
        tools=ToolClient(ToolRegistry.load(settings.tools_path), governor),
        gateway_url=settings.gateway_url,
    )
    agents = AgentRegistry(settings.agents_dir)
    return Activities(deps, agents, settings.checkpoint_dsn, make_online_scorer(governor, agents))


async def run(settings: OrchestratorSettings) -> None:
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    activities = build_activities(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with ThreadPoolExecutor(max_workers=settings.activity_threads) as executor:
        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[AgentRunWorkflow],
            activities=activities.all(),
            activity_executor=executor,
        )
        get_logger().info("worker started", task_queue=settings.task_queue)
        await worker.run()


def main() -> None:
    asyncio.run(run(OrchestratorSettings(service_name="orchestrator-worker")))


if __name__ == "__main__":
    main()
