"""M16 cross-repository links, M18 query contract, executor and the read-only connector."""

import asyncio
from pathlib import Path

import pytest
import yaml
from hapm_config import ProjectConfig
from hapm_sentinel.linkers import cross_repo, satisfies
from hapm_sentinel.memstore import MemoryGraphStore
from hapm_sentinel.model import Edge, Node
from hapm_sentinel.query import Executor, QueryContract
from pydantic import ValidationError

from tests.unit.sentinel.conftest import ROOT, RepoGraph


def test_cross_repo_links_match_truth_and_carry_evidence(links: list[Edge]) -> None:
    truth = {
        tuple(t)
        for t in yaml.safe_load((ROOT / "tests/fixtures/sentinel/truth.yaml").read_text(encoding="utf-8"))["links"]
    }
    got = {(e.source, e.relation, e.target) for e in links if not e.source.startswith("svc:")}
    assert got == truth
    assert all(e.cross_repo and e.confidence == "EXTRACTED" and e.file and e.line_start for e in links)


def test_jira_keys_topics_tables_and_tests(fleet: dict[str, RepoGraph]) -> None:
    orders = fleet["orders-service"]
    assert {"ticket:ORD-12", "ticket:ORD-7"} <= set(orders.nodes)
    rels = {(e.source, e.relation, e.target) for e in orders.edges}
    assert ("orders-service:orders/service.py#OrderService.create", "PUBLISHES", "topic:orders.created") in rels
    assert ("orders-service:orders/models.py#Order", "REFERENCES", "table:orders") in rels
    assert orders.nodes["orders-service:orders/models.py#Order"].type == "DataModel"
    inv = {(e.source, e.relation, e.target) for e in fleet["inventory-service"].edges}
    assert (
        "inventory-service:inventory/stock.py#available",
        "TESTED_BY",
        "inventory-service:tests/test_stock.py#StockTest.test_unknown_sku",
    ) in inv


def test_version_mismatch_and_unknown_host_are_never_extracted(
    project: ProjectConfig, fleet: dict[str, RepoGraph]
) -> None:
    views = {n: g.view for n, g in fleet.items()}
    orders_manifest = views["orders-service"].facts.manifests[0]
    original = dict(orders_manifest["dep_specs"])
    orders_manifest["dep_specs"]["acme-shared"] = "acme-shared>=2.0"
    calls = list(views["orders-service"].facts.http_calls)
    views["orders-service"].facts.http_calls.append(
        {"file": "orders/x.py", "line": 1, "symbol": None, "method": "GET", "url": "http://unknown-host/nothing"}
    )
    try:
        result = cross_repo(views, project.sentinel)
    finally:
        orders_manifest["dep_specs"] = original
        views["orders-service"].facts.http_calls[:] = calls
    dep = [e for e in result.edges if e.relation == "DEPENDS_ON" and e.source == "repo:orders-service"]
    assert dep and dep[0].confidence == "AMBIGUOUS"
    assert all(
        e.confidence == "AMBIGUOUS"
        for e in result.edges
        if e.repo == "orders-service" and e.relation in ("IMPORTS", "CALLS")
    )
    assert result.report["orders-service"]["unresolved"][0]["url"] == "http://unknown-host/nothing"


def test_version_specs() -> None:
    assert satisfies("1.2.0", "acme-shared>=1.2") is True
    assert satisfies("1.2.0", ">=2.0") is False
    assert satisfies("1.4.2", "^1.2.0") is True and satisfies("2.0.0", "^1.2.0") is False
    assert satisfies("", ">=1") is None and satisfies("1.0", "latest") is None


def _store() -> MemoryGraphStore:
    n = [
        Node("a:x.py", "Module", "x.py", "a", "s1", "x.py", 1, 9),
        Node("a:x.py#caller", "Function", "caller", "a", "s1", "x.py", 1, 5),
        Node("a:x.py#helper", "Function", "helper", "a", "s1", "x.py", 6, 9),
        Node("b:y.py#client", "Function", "client", "b", "s2", "y.py", 1, 3),
        Node("b:y.py#dup", "Function", "dup", "b", "s2", "y.py", 4, 5),
        Node("a:z.py#dup", "Function", "dup", "a", "s1", "z.py", 1, 2),
        Node("a:tests/t.py#test_helper", "Test", "test_helper", "a", "s1", "tests/t.py", 1, 2),
    ]
    e = [
        Edge("a:x.py", "a:x.py#caller", "CONTAINS", "EXTRACTED", "a", "s1", "x.py", 1, 1),
        Edge("a:x.py", "a:x.py#helper", "CONTAINS", "EXTRACTED", "a", "s1", "x.py", 6, 6),
        Edge("a:x.py#caller", "a:x.py#helper", "CALLS", "EXTRACTED", "a", "s1", "x.py", 3, 3),
        Edge("b:y.py#client", "a:x.py#caller", "CALLS", "INFERRED", "b", "s2", "y.py", 2, 2, cross_repo=True),
        Edge("a:x.py#helper", "a:tests/t.py#test_helper", "TESTED_BY", "INFERRED", "a", "s1", "tests/t.py", 1, 1),
        Edge(
            "b:y.py#dup", "a:x.py#helper", "CALLS", "EXTRACTED", "b", "s2", "y.py", 99, 99
        ),  # cites a line that does not exist
    ]
    return MemoryGraphStore(n, e, {("a", "s1", "x.py"): 9, ("b", "s2", "y.py"): 5, ("a", "s1", "tests/t.py"): 2})


def test_impact_walks_backwards_across_repos_and_lists_tests() -> None:
    result = Executor(_store()).run(QueryContract(intent="impact", entities=["helper"]))
    assert result["status"] == "answered"
    assert "caller" in result["answer"] and "client" in result["answer"]
    assert result["tests"] == ["a:tests/t.py#test_helper"]
    assert result["dropped_evidence"] == 1  # the edge citing y.py:99 is dropped, never shown
    assert all(ev["line_end"] <= 9 for ev in result["evidence"] if ev["file"] == "x.py")


def test_path_reports_weakest_hop_and_statuses() -> None:
    ex = Executor(_store())
    result = ex.run(QueryContract(intent="path", entities=["client"], target="helper", depth=3))
    assert result["status"] == "answered" and result["path_confidence"] == "INFERRED"
    assert ex.run(QueryContract(intent="explain", entities=["nothing"]))["status"] == "not_found"
    ambiguous = ex.run(QueryContract(intent="explain", entities=["dup"]))
    assert ambiguous["status"] == "ambiguous_entity" and len(ambiguous["candidates"]) == 2
    assert ex.run(QueryContract(intent="explain", entities=["dup"], repos=["a"]))["status"] == "answered"
    strict = ex.run(QueryContract(intent="path", entities=["client"], target="helper", min_confidence="EXTRACTED"))
    assert strict["status"] == "not_found"


def test_contract_is_strict_and_budget_truncates() -> None:
    with pytest.raises(ValidationError):
        QueryContract.model_validate({"intent": "impact", "entities": ["x"], "tool": "github_push_branch"})
    with pytest.raises(ValidationError):
        QueryContract(intent="impact", entities=["x"], depth=9)
    big = MemoryGraphStore(
        [Node(f"r:f.py#n{i}", "Function", f"node{i}", "r", "s", "f.py", 1, 1) for i in range(300)],
        [],
        {("r", "s", "f.py"): 1},
    )
    result = Executor(big).run(QueryContract(intent="find", entities=["node"], budget_tokens=100))
    assert result["truncated"] is True


def test_connector_exposes_only_read_tools(project: ProjectConfig) -> None:
    from hapm_sentinel_mcp.main import READ_TOOLS, build

    server = build(project, _store(), ["127.0.0.1:8106"], lambda: True)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == set(READ_TOOLS)
    assert not [
        n for n in names if any(w in n for w in ("push", "create", "delete", "write", "merge", "update", "comment"))
    ]
    registry = yaml.safe_load((ROOT / "connectors/tools.yaml").read_text(encoding="utf-8"))
    sentinel = [t for t in registry["tools"] if t["server"] == "sentinel"]
    assert {t["name"] for t in sentinel} == set(READ_TOOLS) and all(t["kind"] == "read" for t in sentinel)


def test_context_pack_names_cross_repo_impact(project: ProjectConfig) -> None:
    from hapm_sentinel_mcp.main import build

    server = build(project, _store(), ["127.0.0.1:8106"], lambda: True)
    content, structured = asyncio.run(server.call_tool("sentinel_context_pack", {"repo": "a", "files": ["x.py"]}))
    pack = structured if isinstance(structured, dict) else {}
    pack = pack.get("result", pack)
    assert pack["cross_repo"] == ["b"] and "a:tests/t.py#test_helper" in pack["tests"]


def test_developer_agent_may_call_sentinel_read_tools() -> None:
    agent = yaml.safe_load(Path(ROOT / "agents/developer.yaml").read_text(encoding="utf-8"))
    assert {"sentinel_context_pack", "sentinel_impact"} <= set(agent["allowed_tools"])
