"""Offline Sentinel pipeline for unit tests: fixtures, recorded engine outputs, no database, no network."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hapm_config import ProjectConfig, load_project
from hapm_sentinel.build import run_engines
from hapm_sentinel.engines.runner import RecordedRunner
from hapm_sentinel.facts import RepoFacts, repo_facts
from hapm_sentinel.linkers import RepoView, cross_repo, derive_tests, intra_repo
from hapm_sentinel.merge import Merged, merge
from hapm_sentinel.model import Edge, Node
from hapm_sentinel.ontology import ontology
from hapm_sentinel.sources import LocalReader

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_REPOS = ["shared-lib", "orders-service", "inventory-service", "web-portal"]


class NullStore:
    """Enough of SentinelStore for run_engines: an always-empty cache."""

    def cache_get(self, kind: str, key: str) -> Any:
        return None

    def cache_put(self, kind: str, key: str, value: Any) -> None:
        return None


@dataclass
class RepoGraph:
    view: RepoView
    nodes: dict[str, Node]
    edges: list[Edge]
    merged: Merged
    engines: dict[str, Any]
    tier: str


@dataclass
class _Ctx:
    project: ProjectConfig
    store: Any
    runner: Any


def offline_repo(project: ProjectConfig, name: str) -> RepoGraph:
    repo = project.repo(name)
    source = LocalReader().read(repo)
    snap = source.snapshot
    facts: RepoFacts = repo_facts(snap.files)
    ctx = _Ctx(project, NullStore(), RecordedRunner(ROOT / project.sentinel.engines.recorded_dir))
    fragments, engines, tier = run_engines(ctx, repo, snap, facts, force=True)  # type: ignore[arg-type]
    merged = merge(fragments)
    scip = [f for f in fragments if f.engine == "scip"]
    view = RepoView(
        name,
        snap.sha,
        repo,
        merged.nodes,
        facts,
        source.commits,
        {k: v for f in scip for k, v in f.exports.items()},
        [r for f in scip for r in f.external_refs],
    )
    extra_nodes, extra_edges = intra_repo(view)
    for node in extra_nodes.values():
        merged.nodes.setdefault(node.id, node)
    edges = merged.edge_list() + extra_edges
    edges += derive_tests(view, edges)
    kept, _ = ontology().conform(merged.nodes, edges)
    return RepoGraph(view, merged.nodes, kept, merged, engines, tier)


@pytest.fixture(scope="session")
def project() -> ProjectConfig:
    return load_project(ROOT / "config")


@pytest.fixture(scope="session")
def fleet(project: ProjectConfig) -> dict[str, RepoGraph]:
    return {name: offline_repo(project, name) for name in FIXTURE_REPOS}


@pytest.fixture(scope="session")
def links(project: ProjectConfig, fleet: dict[str, RepoGraph]) -> list[Edge]:
    return cross_repo({n: g.view for n, g in fleet.items()}, project.sentinel).edges
