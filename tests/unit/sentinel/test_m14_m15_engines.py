"""M14/M15: config, snapshots, facts, graphify and SCIP mapping, the ast fallback, merge precedence, budgets."""

import copy
import json

import pytest
import yaml
from hapm_config import ProjectConfig, RepoConfig
from hapm_config.models import RepoSentinel, SentinelConfig
from hapm_sentinel.build import apply_budget, code_languages
from hapm_sentinel.engines import ast_py
from hapm_sentinel.engines.graphify import GraphifySchemaError, check_schema, map_graph
from hapm_sentinel.engines.runner import RecordedRunner
from hapm_sentinel.engines.scip import _is_call, map_index, parse_symbol
from hapm_sentinel.facts import repo_facts
from hapm_sentinel.ids import endpoint_id, normalise_route, route_pattern
from hapm_sentinel.merge import merge
from hapm_sentinel.model import Edge, Fragment, Node
from hapm_sentinel.ontology import ontology
from hapm_sentinel.sources import LocalReader, fixture_head

from tests.unit.sentinel.conftest import ROOT, RepoGraph

RECORDED = ROOT / "tests/fixtures/sentinel/recorded"


def _snap(project: ProjectConfig, name: str):  # type: ignore[no-untyped-def]
    return LocalReader().read(project.repo(name)).snapshot


def test_config_defaults_and_validation(project: ProjectConfig) -> None:
    assert project.sentinel.llm == "off"  # the first production launch makes no model calls
    assert project.sentinel.engines.graphify.version == "0.9.115"
    assert {"python", "typescript"} <= set(project.sentinel.engines.scip.indexers)
    assert project.repo("orders-service").sentinel.enabled
    with pytest.raises(ValueError):
        RepoSentinel(tiers=[4])
    assert SentinelConfig().export.html_node_limit == 7000
    assert SentinelConfig().enrichment.label_retries == 3


def test_snapshot_excludes_secrets_and_matches_mock_github_head(project: ProjectConfig) -> None:
    source = LocalReader().read(project.repo("payments-service"))
    assert "secrets/config_local.py" not in source.snapshot.files
    folder = ROOT / "tests/fixtures/repos/payments_service"
    every_file = {
        f.relative_to(folder).as_posix(): f.read_text(encoding="utf-8") for f in folder.rglob("*") if f.is_file()
    }
    assert source.snapshot.sha == fixture_head(every_file)  # the SHA the mock GitHub backend reports
    orders = LocalReader().read(project.repo("orders-service"))
    assert [c["message"] for c in orders.commits][0].startswith("ORD-12")


def test_facts_find_endpoints_calls_topics_tables_and_manifests(project: ProjectConfig) -> None:
    facts = repo_facts(_snap(project, "orders-service").files)
    assert {(e["method"], e["path"]) for e in facts.endpoints} == {("POST", "/orders"), ("GET", "/orders/{order_id}")}
    assert {c["url"] for c in facts.http_calls} == {
        "{INVENTORY_URL}/inventory/{sku}",
        "{INVENTORY_URL}/inventory/{sku}/reservations",
    }
    assert facts.constants["orders/config.py"]["INVENTORY_URL"] == "http://inventory-service:8000"
    assert [(t["topic"], t["direction"]) for t in facts.topics] == [("orders.created", "PUBLISHES")]
    assert {(t["table"], t["access"]) for t in facts.tables} == {("orders", "READS"), ("orders", "WRITES")}
    assert facts.manifests[0]["name"] == "orders-service" and "acme-shared" in facts.manifests[0]["deps"]
    assert facts.manifests[0]["dep_lines"]["acme-shared"] == 5
    ts = repo_facts(_snap(project, "web-portal").files)
    assert {(c["method"], c["url"]) for c in ts.http_calls} == {
        ("POST", "{ORDERS_URL}/orders?sku={sku}&quantity={quantity}&price={price}"),
        ("GET", "{ORDERS_URL}/orders/{orderId}"),
    }
    inv = repo_facts(_snap(project, "inventory-service").files)
    assert [(t["topic"], t["direction"], t["symbol"]) for t in inv.topics] == [
        ("orders.created", "SUBSCRIBES", "on_order_created")
    ]


def test_routes_normalise_and_match() -> None:
    assert normalise_route("/orders/<int:order_id>/") == "/orders/{order_id}"
    assert normalise_route("/orders/:id") == "/orders/{id}"
    assert route_pattern("/inventory/{sku}").match("/inventory/x")
    assert not route_pattern("/inventory/{sku}").match("/inventory/x/reservations")
    assert endpoint_id("r", "get", "/a/:b") == "r:api#GET /a/{b}"


def test_graphify_schema_probe_fails_loudly_on_change(project: ProjectConfig) -> None:
    graph = json.loads((RECORDED / "orders_service/graphify.json").read_text(encoding="utf-8"))
    check_schema(graph, "0.9.115")
    changed = copy.deepcopy(graph)
    changed["graph"]["schema_version"] = 2
    with pytest.raises(GraphifySchemaError):
        check_schema(changed, "0.9.115")
    with pytest.raises(GraphifySchemaError):
        check_schema(graph, "0.9.200")  # a different graphify version than the pinned one
    broken = copy.deepcopy(graph)
    del broken["links"][0]["confidence"]
    with pytest.raises(GraphifySchemaError):
        check_schema(broken, "0.9.115")


def test_graphify_mapping_keeps_confidence_and_rejects_unmapped(project: ProjectConfig) -> None:
    snap = _snap(project, "orders-service")
    graph = json.loads((RECORDED / "orders_service/graphify.json").read_text(encoding="utf-8"))
    frag = map_graph(graph, snap, "0.9.115", project.sentinel.relation_map)
    edges = {(e.source, e.relation, e.target): e for e in frag.edges}
    call = edges[
        ("orders-service:orders/api.py#create_order", "CALLS", "orders-service:orders/service.py#OrderService.create")
    ]
    assert call.confidence == "INFERRED"  # graphify's own tag, unchanged
    assert ("orders-service:orders/service.py", "IMPORTS", "orders-service:orders/models.py") in edges
    assert frag.nodes["orders-service:orders/service.py#OrderService.create"].type == "Function"
    assert any("rationale_for" in k for k in frag.rejected)  # docstrings kept as data, not edges
    assert "SYSTEM:" in frag.nodes["orders-service:orders/service.py"].attrs["doc"]  # injection text stored as data
    lessmap = dict(project.sentinel.relation_map)
    del lessmap["calls"]
    frag2 = map_graph(graph, snap, "0.9.115", lessmap)
    assert frag2.rejected["edge: unmapped graphify relation calls"] > 0
    assert not any(e.relation == "CALLS" for e in frag2.edges)


def test_scip_symbol_parsing() -> None:
    sym = parse_symbol("scip-python python orders-service 0.4.0 `orders.service`/OrderService#create().")
    assert (
        sym is not None
        and sym.namespace == ("orders.service",)
        and sym.qualname == "OrderService.create"
        and sym.kind == "method"
    )
    ts = parse_symbol("scip-typescript npm web-portal 2.0.0 src/`ordersApi.ts`/placeOrder().")
    assert ts is not None and ts.namespace == ("src", "ordersApi.ts") and ts.qualname == "placeOrder"
    assert parse_symbol("local 3") is None
    assert _is_call("x = f(1)\n", [0, 4, 5]) and not _is_call("return f\n", [0, 7, 8])


def test_scip_mapping_exact_ranges_and_external_refs(project: ProjectConfig) -> None:
    snap = _snap(project, "orders-service")
    index = json.loads((RECORDED / "orders_service/scip-python.json").read_text(encoding="utf-8"))
    frag = map_index(index, snap, "scip-python")
    create = frag.nodes["orders-service:orders/service.py#OrderService.create"]
    assert (create.line_start, create.line_end) == (20, 27)  # full range, which graphify does not give
    calls = {(e.source, e.target) for e in frag.edges if e.relation == "CALLS"}
    assert (
        "orders-service:orders/service.py#OrderService.create",
        "orders-service:orders/inventory_client.py#reserve",
    ) in calls
    assert all(e.confidence == "EXTRACTED" for e in frag.edges)
    modules = {r["module"] for r in frag.external_refs}
    assert {"acme_shared.events", "acme_shared.money"} <= modules  # resolved later by the cross-repo linker


def test_ast_fallback_matches_truth_for_python(project: ProjectConfig) -> None:
    frag = ast_py.extract(_snap(project, "inventory-service"))
    calls = {(e.source, e.target) for e in frag.edges if e.relation == "CALLS"}
    assert ("inventory-service:inventory/api.py#get_stock", "inventory-service:inventory/stock.py#available") in calls
    orders = ast_py.extract(_snap(project, "orders-service"))
    inferred = {(e.source, e.target): e.confidence for e in orders.edges if e.relation == "CALLS"}
    # Resolved through a module-level instance of an imported class: INFERRED, never EXTRACTED.
    assert (
        inferred[("orders-service:orders/api.py#create_order", "orders-service:orders/service.py#OrderService.create")]
        == "INFERRED"
    )


def test_merge_precedence_and_disagreement_logging() -> None:
    def frag(engine: str, conf: str, extra: bool = False) -> Fragment:
        f = Fragment(engine, "1", "r", "s")
        for nid in ("r:a.py#f", "r:a.py#g", "r:a.py#h"):
            f.add_node(Node(nid, "Function", nid, "r", "s", "a.py", 1, 2, provenance=[f"{engine}@1"]))
        f.add_edge(Edge("r:a.py#f", "r:a.py#g", "CALLS", conf, "r", "s", "a.py", 1, 1, [f"{engine}@1"]))
        if extra:
            f.add_edge(Edge("r:a.py#f", "r:a.py#h", "CALLS", "INFERRED", "r", "s", "a.py", 2, 2, [f"{engine}@1"]))
        return f

    merged = merge([frag("graphify", "INFERRED", extra=True), frag("scip", "EXTRACTED")])
    edge = merged.edges[("r:a.py#f", "r:a.py#g", "CALLS")]
    assert edge.confidence == "EXTRACTED" and {"graphify@1", "scip@1"} <= set(edge.provenance)
    assert merged.disagreements == {"agree": 1, "graphify_only": 1}


def test_budget_hides_functions_and_drops_excluded_tiers() -> None:
    nodes = {f"n{i}": Node(f"n{i}", "Function", "f", "r", "s", tier=3) for i in range(20)}
    nodes["m"] = Node("m", "Module", "m", "r", "s", tier=2)
    repo = RepoConfig(name="r", jira_project="RR", sentinel=RepoSentinel(node_budget=10))
    kept, _, info = apply_budget(dict(nodes), [], repo)
    assert info["hidden_over_budget"] == 20 and all(not n.in_default for n in kept.values() if n.tier == 3)
    repo2 = RepoConfig(name="r", jira_project="RR", sentinel=RepoSentinel(tiers=[1, 2]))
    kept2, _, info2 = apply_budget(dict(nodes), [], repo2)
    assert set(kept2) == {"m"} and info2["dropped_by_tier"] == 20


def test_recorded_runner_detects_stale_recordings(project: ProjectConfig) -> None:
    repo = project.repo("orders-service")
    snap = _snap(project, "orders-service")
    runner = RecordedRunner(RECORDED)
    assert runner.graphify(repo, snap).status == "ok"
    snap.sha = "0" * 40
    assert runner.graphify(repo, snap).status == "stale"
    assert code_languages(_snap(project, "web-portal")) == ["typescript"]


def test_every_fixture_builds_with_exact_precision(fleet: dict[str, RepoGraph]) -> None:
    for name, graph in fleet.items():
        assert graph.tier == "exact", (name, graph.engines)
        types = {n.id: n.type for n in graph.nodes.values()}
        assert all(ontology().edge_problem(e, types) is None for e in graph.edges)


def test_structural_truth_on_fixtures(fleet: dict[str, RepoGraph]) -> None:
    """Validator 3 offline: internal IMPORTS and function CALLS against the hand-written truth."""
    truth = yaml.safe_load((ROOT / "tests/fixtures/sentinel/truth.yaml").read_text(encoding="utf-8"))
    expected, predicted = set(), set()
    for repo, rels in truth["edges"].items():
        expected |= {(f"{repo}:{a}", "IMPORTS", f"{repo}:{b}") for a, b in rels.get("IMPORTS", [])}
        expected |= {(f"{repo}:{a}", "CALLS", f"{repo}:{b}") for a, b in rels.get("CALLS", [])}
        g = fleet[repo]
        for e in g.edges:
            if e.relation == "IMPORTS" and not e.target.startswith("lib:"):
                predicted.add((e.source, "IMPORTS", e.target))
            elif e.relation == "CALLS" and g.nodes[e.target].type in ("Function", "Test"):
                predicted.add((e.source, "CALLS", e.target))
    tp = predicted & expected
    precision, recall = len(tp) / len(predicted), len(tp) / len(expected)
    assert precision >= 0.95, sorted(predicted - expected)
    assert recall >= 0.90, sorted(expected - predicted)
