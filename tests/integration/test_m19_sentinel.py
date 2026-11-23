"""Phase 2 integration: Sentinel against the running stack (Postgres, gateway, Temporal worker, connector).

Offline inputs only: fixture repositories, recorded engine outputs, the Fake model provider.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from hapm_config import load_project
from hapm_mcp_client.client import mcp_transport
from hapm_sentinel import validate as v
from hapm_sentinel.build import BuildContext, build_fleet, build_repo, plan_fleet
from hapm_sentinel.engines.runner import runner_for
from hapm_sentinel.enrich import enrich_repo
from hapm_sentinel.llm import GatewayLLM
from hapm_sentinel.ontology import ontology
from hapm_sentinel.planner_eval import golden_planner, templated_golden, vector_baseline
from hapm_sentinel.query import Executor, QueryContract
from hapm_sentinel.sources import ConnectorReader, LocalReader
from hapm_sentinel.store import SentinelStore
from sqlalchemy import Engine

from tests.integration.conftest import ROOT


@pytest.fixture(scope="module")
def ctx(app_engine: Engine) -> Iterator[BuildContext]:
    project = load_project(ROOT / "config")
    context = BuildContext(
        project, SentinelStore(app_engine), LocalReader(), runner_for(project.sentinel.engines), ontology()
    )
    result = build_fleet(context, force=True)
    assert all(r["status"] == "succeeded" for r in result["repos"]), result["repos"]
    yield context


def test_fleet_builds_exact_and_links_with_evidence(ctx: BuildContext) -> None:
    stats = ctx.store.stats()["repos"]
    assert {"orders-service", "inventory-service", "shared-lib", "web-portal", "payments-service"} <= set(stats)
    runs = [r for r in ctx.store.runs(limit=20) if r["status"] == "succeeded"]
    assert all(r["precision_tier"] == "exact" for r in runs[:5])
    nodes, edges = ctx.store.current_graph()
    links = [e for e in edges if e.layer == "link"]
    assert len(links) == 16 and all(e.confidence == "EXTRACTED" for e in links)


def test_incremental_build_skips_unchanged_and_dry_run_estimates(ctx: BuildContext) -> None:
    again = build_repo(ctx, "orders-service")
    assert again["status"] == "unchanged"
    plan = plan_fleet(ctx)
    assert all(p["action"] == "skip (unchanged)" for p in plan) and all(p["estimated_llm_tokens"] == 0 for p in plan)


def test_recursive_cte_queries_and_evidence_check(ctx: BuildContext) -> None:
    ex = Executor(ctx.store)
    impact = ex.run(QueryContract(intent="impact", entities=["get_stock"]))
    assert impact["status"] == "answered" and "orders-service" in impact["impacted_repos"]
    assert impact["dropped_evidence"] == 0 and impact["evidence"]
    path = ex.run(QueryContract(intent="path", entities=["placeOrder"], target="create_order", depth=4))
    assert path["status"] == "answered" and path["path_confidence"] == "EXTRACTED"
    assert ex.run(QueryContract(intent="explain", entities=["to_cents"]))["status"] == "ambiguous_entity"
    assert ex.run(QueryContract(intent="explain", entities=["to_cents"], repos=["shared-lib"]))["status"] == "answered"
    coverage = ex.run(QueryContract(intent="coverage", entities=["repo:inventory-service"]))
    assert coverage["status"] == "answered"


def test_validators_pass_on_the_fixture_fleet(ctx: BuildContext, tmp_path: Path) -> None:
    nodes, edges = ctx.store.current_graph()
    counts = ctx.store.line_counts()
    runs = [r for r in ctx.store.runs(limit=200) if r["status"] == "succeeded"]
    assert v.v1_schema(ctx.ontology, nodes, edges).passed
    assert v.v2_integrity(nodes, edges).passed
    assert v.v3_structural(nodes, edges, runs).passed
    assert v.v4_evidence(edges, counts).passed
    sample = v.v5_confidence(edges, tmp_path / "sample.yaml")
    assert sample.passed and (tmp_path / "sample.yaml").exists()
    reviewed = tmp_path / "reviewed.yaml"
    data = json.loads(json.dumps(__import__("yaml").safe_load((tmp_path / "sample.yaml").read_text(encoding="utf-8"))))
    for e in data["edges"]:
        e["verdict"] = "correct"
    reviewed.write_text(__import__("yaml").safe_dump(data), encoding="utf-8")
    assert "measured precision 1.00" in v.v5_confidence(edges, tmp_path / "s2.yaml", reviewed).value
    golden = v.load_golden()
    files = {n: LocalReader().read(r).snapshot.files for n, r in ctx.project.repos.items() if r.sentinel.enabled}
    planner = v.v6_contract(golden + templated_golden(), golden_planner(ctx.project))
    assert planner.label == "plumbing only (Fake provider)" and len(golden + templated_golden()) == 50
    answers = v.v7_answers(ctx.store, golden, files, vector_baseline(ctx.project, ctx.store, files))
    assert answers.passed, answers.value


def test_enrichment_through_the_gateway_records_the_provider(ctx: BuildContext) -> None:
    project = ctx.project.model_copy(deep=True)
    project.sentinel.llm = "local"
    project.sentinel.enrichment.backoff_seconds = 0
    repo = project.repo("shared-lib")
    head = ctx.store.heads()["shared-lib"]
    nodes = ctx.store.repo_nodes("shared-lib", head["sha"])
    edges = ctx.store.repo_edges("shared-lib", head["sha"], "repo")
    snap = LocalReader().read(repo).snapshot
    ctx.store.cache_clear("enrich")
    stats = enrich_repo(project, repo, ctx.store, snap, nodes, edges, GatewayLLM(run_id="itest-enrich"))
    assert stats.enriched > 0 and stats.providers == {"fake": stats.enriched}
    assert stats.embedded > 0  # Fake embeddings: 1536 dimensions in sentinel_embeddings, not code_chunks
    enriched = [n for n in ctx.store.repo_nodes("shared-lib", head["sha"]).values() if n.summary]
    assert enriched and all(n.attrs["enrichment"]["provider"] == "fake" for n in enriched)


def test_connector_reader_uses_only_read_tools(ctx: BuildContext) -> None:
    reader = ConnectorReader()
    source = reader.read(ctx.project.repo("orders-service"))
    assert source.snapshot.sha == LocalReader().read(ctx.project.repo("orders-service")).snapshot.sha
    assert source.commits[0]["message"].startswith("ORD-12")
    with pytest.raises(PermissionError):
        reader._tool("github_push_branch", {})


def test_live_connector_context_pack() -> None:
    _, pack = asyncio.run(
        mcp_transport(
            "http://127.0.0.1:8106/mcp",
            "sentinel_context_pack",
            {"repo": "inventory-service", "files": ["inventory/api.py"]},
            30,
        )
    )
    assert pack["cross_repo"] == ["orders-service"] and pack["evidence"]


def test_temporal_fleet_workflow_and_pilot_gate(ctx: BuildContext) -> None:
    from hapm_sentinel.pilot import readiness, run_pilot
    from hapm_sentinel.workflows import start_fleet

    result = start_fleet(["shared-lib"], False, "recorded", "connector")
    assert result["repos"][0]["status"] in ("succeeded", "unchanged") and result["links"]["edges"] == 16
    missing = readiness(ctx.project, rehearsal=False)
    assert any("mock backend" in m for m in missing)
    assert run_pilot("1", rehearsal=False) == 2
    assert run_pilot("5", rehearsal=True, store=ctx.store) == 0
