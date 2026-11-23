"""The versioned Sentinel ontology (config/ontology.yaml) and conformance checks."""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from hapm_sentinel.model import CONFIDENCE_RANK, Edge, Node

DEFAULT_PATH = Path(__file__).resolve().parents[4] / "config" / "ontology.yaml"


@dataclass(frozen=True)
class Relation:
    name: str
    sources: frozenset[str]
    targets: frozenset[str]


@dataclass(frozen=True)
class Ontology:
    version: str
    tiers: dict[str, int]
    relations: dict[str, Relation]

    @classmethod
    def load(cls, path: Path | None = None) -> "Ontology":
        data = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
        tiers = {name: int(spec["tier"]) for name, spec in data["node_types"].items()}
        relations = {
            name: Relation(name, frozenset(spec["sources"]), frozenset(spec["targets"]))
            for name, spec in data["relations"].items()
        }
        for rel in relations.values():
            unknown = (rel.sources | rel.targets) - set(tiers)
            if unknown:
                raise ValueError(f"relation {rel.name} names unknown node types {sorted(unknown)}")
        return cls(str(data["version"]), tiers, relations)

    def tier(self, node_type: str) -> int:
        return self.tiers[node_type]

    def node_problem(self, node: Node) -> str | None:
        if node.type not in self.tiers:
            return f"unknown node type {node.type}"
        if not node.id or not node.label:
            return "node without id or label"
        if node.line_start is not None and node.line_end is not None and node.line_end < node.line_start:
            return "line_end before line_start"
        return None

    def edge_problem(self, edge: Edge, types: dict[str, str]) -> str | None:
        rel = self.relations.get(edge.relation)
        if rel is None:
            return f"unknown relation {edge.relation}"
        if edge.confidence not in CONFIDENCE_RANK:
            return f"unknown confidence {edge.confidence}"
        source, target = types.get(edge.source), types.get(edge.target)
        if source is None or target is None:
            return "dangling edge"
        if source not in rel.sources or target not in rel.targets:
            return f"{edge.relation} not allowed from {source} to {target}"
        return None

    def conform(self, nodes: dict[str, Node], edges: list[Edge]) -> tuple[list[Edge], Counter[str]]:
        """Drop non-conforming nodes and edges in place of the caller; return kept edges and rejection counts."""
        rejected: Counter[str] = Counter()
        for node_id in [n for n, node in nodes.items() if self.node_problem(node)]:
            rejected[f"node: {self.node_problem(nodes[node_id])}"] += 1
            del nodes[node_id]
        for node in nodes.values():
            node.tier = self.tier(node.type)
        types = {n.id: n.type for n in nodes.values()}
        kept: list[Edge] = []
        for edge in edges:
            problem = self.edge_problem(edge, types)
            if problem:
                rejected[f"edge: {problem}"] += 1
            else:
                kept.append(edge)
        return kept, rejected


@lru_cache(maxsize=4)
def ontology(path: str | None = None) -> Ontology:
    return Ontology.load(Path(path) if path else None)
