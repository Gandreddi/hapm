"""Sentinel MCP server (read-only). Agents reach it through the MCP client, so every call is policy-checked
and audited like any other tool. No write tool exists; tests/unit/sentinel asserts this.
"""

from typing import Any

from hapm_config import ProjectConfig, load_project
from hapm_core.db import make_engine
from hapm_core.settings import CoreSettings
from hapm_mcp_client.server import build_server
from hapm_sentinel.ask import ask
from hapm_sentinel.query import Executor, GraphStore, QueryContract
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

READ_TOOLS = (
    "sentinel_query",
    "sentinel_ask",
    "sentinel_explain",
    "sentinel_path",
    "sentinel_impact",
    "sentinel_neighbors",
    "sentinel_find",
    "sentinel_coverage",
    "sentinel_stats",
    "sentinel_context_pack",
)


class SentinelSettings(CoreSettings):
    service_name: str = "sentinel-connector"
    mcp_allowed_hosts: list[str] = ["sentinel-connector:8000", "127.0.0.1:8106", "localhost:8106"]


def build(project: ProjectConfig, store: GraphStore, hosts: list[str], ready: Any, stats: Any = None) -> FastMCP:
    mcp = build_server("hapm-sentinel", hosts, ready)
    executor = Executor(store)

    def run(**contract: Any) -> dict[str, Any]:
        return executor.run(QueryContract(**{k: v for k, v in contract.items() if v is not None}))

    @mcp.tool()
    def sentinel_query(
        intent: str,
        entities: list[str],
        target: str | None = None,
        repos: list[str] | None = None,
        relations: list[str] | None = None,
        depth: int = 2,
        budget_tokens: int = 2000,
        min_confidence: str = "INFERRED",
    ) -> dict[str, Any]:
        """Run the JSON query contract (intent: find | explain | path | impact | coverage). Deterministic; no model."""
        return run(
            intent=intent,
            entities=entities,
            target=target,
            repos=repos,
            relations=relations,
            depth=depth,
            budget_tokens=budget_tokens,
            min_confidence=min_confidence,
        )

    @mcp.tool()
    def sentinel_ask(question: str) -> dict[str, Any]:
        """Plain-language question -> query contract (through the gateway) -> answer. Disabled when llm is off."""
        return ask(project, store, question)

    @mcp.tool()
    def sentinel_explain(entity: str, repos: list[str] | None = None) -> dict[str, Any]:
        """A node, where it lives, and its connections."""
        return run(intent="explain", entities=[entity], repos=repos)

    @mcp.tool()
    def sentinel_path(source: str, target: str, depth: int = 4, repos: list[str] | None = None) -> dict[str, Any]:
        """Shortest connection between two nodes; path_confidence is the weakest hop."""
        return run(intent="path", entities=[source], target=target, depth=depth, repos=repos)

    @mcp.tool()
    def sentinel_impact(entity: str, depth: int = 2, repos: list[str] | None = None) -> dict[str, Any]:
        """Everything that depends on a node (callers, importers, consumers, subscribers) and the tests to run."""
        return run(intent="impact", entities=[entity], depth=depth, repos=repos)

    @mcp.tool()
    def sentinel_neighbors(entity: str, relations: list[str] | None = None) -> dict[str, Any]:
        """Direct connections of a node in both directions, optionally only some relations."""
        return run(intent="explain", entities=[entity], relations=relations, depth=1)

    @mcp.tool()
    def sentinel_find(text: str, repos: list[str] | None = None) -> dict[str, Any]:
        """Nodes whose name or id contains the text."""
        return run(intent="find", entities=[text], repos=repos)

    @mcp.tool()
    def sentinel_coverage(entity: str) -> dict[str, Any]:
        """Code without a TESTED_BY edge in a repository (repo:<name>), module or class."""
        return run(intent="coverage", entities=[entity])

    @mcp.tool()
    def sentinel_stats() -> dict[str, Any]:
        """Per-repository node and edge counts, confidence mix and cross-repository edges."""
        return dict(stats()) if stats else {}

    @mcp.tool()
    def sentinel_context_pack(repo: str, files: list[str], depth: int = 2) -> dict[str, Any]:
        """For files about to be changed: impacted services and callers (other repositories included) and the tests to run."""
        impacted: dict[str, set[str]] = {}
        tests: set[str] = set()
        evidence: list[dict[str, Any]] = []
        for path in files[:20]:
            result = run(intent="impact", entities=[f"{repo}:{path}"], depth=depth, repos=[repo])
            if result["status"] != "answered":
                continue
            for r in result.get("impacted_repos", []):
                impacted.setdefault(r, set())
            for n in result["nodes"]:
                impacted.setdefault(n["repo"], set()).add(n["label"])
            tests.update(result.get("tests", []))
            evidence.extend(result["evidence"][:10])
        return {
            "repo": repo,
            "files": files[:20],
            "impacted": {r: sorted(v)[:30] for r, v in impacted.items()},
            "cross_repo": sorted(r for r in impacted if r != repo),
            "tests": sorted(tests)[:50],
            "evidence": evidence[:40],
        }

    return mcp


def main() -> None:
    from hapm_sentinel.store import SentinelStore
    from hapm_telemetry import configure_logging

    settings = SentinelSettings()
    configure_logging(settings.service_name, settings.log_level)
    engine = make_engine(settings.database_url)
    store = SentinelStore(engine)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    build(load_project(), store, settings.mcp_allowed_hosts, ready, store.stats).run(transport="streamable-http")


if __name__ == "__main__":
    main()
