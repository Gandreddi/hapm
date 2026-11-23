"""Graph records shared by every Sentinel engine, linker, store and query."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

Confidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
CONFIDENCE_RANK: dict[str, int] = {"EXTRACTED": 0, "INFERRED": 1, "AMBIGUOUS": 2}
# Lower is stronger. SCIP is compiler-precise, then graphify, then the built-in ast fallback.
ENGINE_RANK: dict[str, int] = {"scip": 0, "graphify": 1, "ast": 2, "linker": 1, "facts": 1}


def engine_of(provenance: str) -> str:
    return provenance.split(":", 1)[0].split("@", 1)[0]


@dataclass
class Node:
    id: str
    type: str
    label: str
    repo: str
    sha: str
    source_file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    tier: int = 2
    provenance: list[str] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    in_default: bool = True
    layer: str = "repo"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "repo": self.repo,
            "sha": self.sha,
            "source_file": self.source_file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "tier": self.tier,
            "summary": self.summary,
            "provenance": list(self.provenance),
            "attrs": dict(self.attrs),
        }


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    confidence: str
    repo: str
    sha: str
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    provenance: list[str] = field(default_factory=list)
    cross_repo: bool = False
    attrs: dict[str, Any] = field(default_factory=dict)
    layer: str = "repo"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.relation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "confidence": self.confidence,
            "repo": self.repo,
            "sha": self.sha,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "provenance": list(self.provenance),
            "cross_repo": self.cross_repo,
        }


@dataclass
class Fragment:
    """What one engine produced for one repository at one SHA."""

    engine: str
    engine_version: str
    repo: str
    sha: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)
    external_refs: list[dict[str, Any]] = field(default_factory=list)  # unresolved cross-repo symbol references
    status: str = "ok"  # ok | unavailable | failed | timeout
    error: str | None = None
    languages: set[str] = field(default_factory=set)  # languages this engine covered
    exports: dict[str, str] = field(default_factory=dict)  # SCIP module namespace -> module node id
    package: str = ""  # SCIP package name the index was built for

    @property
    def provenance(self) -> str:
        return f"{self.engine}@{self.engine_version}"

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if existing.line_end is None and node.line_end is not None:
            existing.line_start, existing.line_end = node.line_start, node.line_end
        return existing

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
