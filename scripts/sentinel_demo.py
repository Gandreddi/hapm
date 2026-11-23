"""make sentinel-demo: build the fixture fleet, link it, query it, and call the live MCP connector.

Offline: fixture repositories, recorded graphify and SCIP outputs, llm off. Needs `make up` for Postgres and
the sentinel-connector service.
"""

import asyncio
import json
import sys
from pathlib import Path

from hapm_config import load_project
from hapm_core.db import make_engine
from hapm_mcp_client.client import mcp_transport
from hapm_sentinel import export
from hapm_sentinel.ask import ask
from hapm_sentinel.build import BuildContext, build_fleet
from hapm_sentinel.cli import database_url
from hapm_sentinel.engines.runner import runner_for
from hapm_sentinel.ontology import ontology
from hapm_sentinel.query import Executor, QueryContract
from hapm_sentinel.sources import LocalReader
from hapm_sentinel.store import SentinelStore

CONNECTOR = "http://127.0.0.1:8106/mcp"
OUT = Path(__file__).resolve().parents[1] / "docs" / "sentinel"


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def main() -> int:
    project = load_project()
    store = SentinelStore(make_engine(database_url()))
    ctx = BuildContext(project, store, LocalReader(), runner_for(project.sentinel.engines), ontology())

    step(
        1,
        f"Building {sum(r.sentinel.enabled for r in project.repos.values())} repositories (llm: {project.sentinel.llm})",
    )
    result = build_fleet(ctx)
    for r in result["repos"]:
        print(
            f"    {r['repo']}: {r['status']} {r.get('precision_tier', '')} nodes={r.get('nodes', '-')} edges={r.get('edges', '-')}"
        )
    print(f"    cross-repository links: {json.dumps(result['links'].get('relations', {}))}")

    step(2, "Querying with the JSON contract (deterministic, no model)")
    executor = Executor(store)
    for contract in (
        QueryContract(intent="impact", entities=["get_stock"]),
        QueryContract(intent="path", entities=["placeOrder"], target="create_order", depth=4),
        QueryContract(intent="explain", entities=["on_order_created"]),
        QueryContract(intent="coverage", entities=["repo:orders-service"]),
    ):
        r = executor.run(contract)
        print(f"    {contract.intent:8} {r['status']}: {r['answer'][:160]}")
        for ev in r["evidence"][:2]:
            print(f"             evidence {ev['repo']}:{ev['file']}:{ev['line_start']} ({ev['confidence']})")

    step(3, "sentinel_ask with llm off")
    print(f"    {ask(project, store, 'What breaks if I change get_stock?')['status']}")

    step(4, "Live MCP connector: tools and a context pack for orders/inventory_client.py")
    try:
        tools = asyncio.run(_list_tools())
        print(f"    tools: {', '.join(sorted(tools))}")
        _, pack = asyncio.run(
            mcp_transport(
                CONNECTOR,
                "sentinel_context_pack",
                {"repo": "orders-service", "files": ["orders/inventory_client.py"]},
                30,
            )
        )
        print(
            f"    impacted repositories: {pack['cross_repo'] or 'none outside orders-service'}; tests: {pack['tests'] or 'none found'}"
        )
    except Exception as exc:  # noqa: BLE001 - the demo reports a down connector instead of failing on it
        print(f"    connector unavailable ({exc}); run make up")
        return 1

    step(5, "Exporting the graph")
    nodes, edges = store.current_graph()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "graph.html").write_text(
        export.to_html(list(nodes.values()), edges, project.sentinel.export.html_node_limit), encoding="utf-8"
    )
    print(f"    {len(nodes)} nodes, {len(edges)} edges -> docs/sentinel/graph.html")
    return 0


async def _list_tools() -> list[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(CONNECTOR) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        return [t.name for t in (await session.list_tools()).tools]


if __name__ == "__main__":
    sys.exit(main())
