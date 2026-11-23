"""Merge engine fragments for one repository.

Precedence for the same (source, target, relation): SCIP EXTRACTED > graphify EXTRACTED > graphify INFERRED >
AMBIGUOUS; the built-in ast engine ranks after graphify. Every contributing engine stays in provenance.
Where SCIP covered a file's language, a CALLS or IMPORTS edge only graphify found is a disagreement: it is
kept (SCIP can miss dynamic code) but logged, and the counts feed validator 3.
"""

from collections import Counter
from dataclasses import dataclass, field

from hapm_sentinel.model import CONFIDENCE_RANK, ENGINE_RANK, Edge, Fragment, Node, engine_of

STRUCTURAL = ("CALLS", "IMPORTS")


@dataclass
class Merged:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    disagreements: Counter[str] = field(default_factory=Counter)
    disagreement_samples: list[dict[str, str]] = field(default_factory=list)

    def edge_list(self) -> list[Edge]:
        return list(self.edges.values())


def _rank(edge: Edge) -> tuple[int, int]:
    best_engine = min((ENGINE_RANK.get(engine_of(p), 9) for p in edge.provenance), default=9)
    return (CONFIDENCE_RANK.get(edge.confidence, 9), best_engine) if best_engine != 0 else (-1, 0)


def merge(fragments: list[Fragment]) -> Merged:
    out = Merged()
    ordered = sorted(fragments, key=lambda f: ENGINE_RANK.get(f.engine, 9))
    for frag in ordered:
        for node in frag.nodes.values():
            existing = out.nodes.get(node.id)
            if existing is None:
                out.nodes[node.id] = Node(
                    **{**node.__dict__, "provenance": list(node.provenance), "attrs": dict(node.attrs)}
                )
                continue
            for p in node.provenance:
                if p not in existing.provenance:
                    existing.provenance.append(p)
            if existing.line_end is None and node.line_end is not None:
                existing.line_start, existing.line_end = node.line_start, node.line_end
            if existing.type in ("Class", "Function") and node.type in ("Test", "DataModel"):
                existing.type = node.type
            for k, v in node.attrs.items():
                existing.attrs.setdefault(k, v)
        for edge in frag.edges:
            existing_edge = out.edges.get(edge.key)
            if existing_edge is None:
                out.edges[edge.key] = Edge(
                    **{**edge.__dict__, "provenance": list(edge.provenance), "attrs": dict(edge.attrs)}
                )
                continue
            merged_prov = existing_edge.provenance + [p for p in edge.provenance if p not in existing_edge.provenance]
            if _rank(edge) < _rank(existing_edge):
                replacement = Edge(**{**edge.__dict__, "provenance": merged_prov, "attrs": dict(edge.attrs)})
                out.edges[edge.key] = replacement
            else:
                existing_edge.provenance = merged_prov
    scip = next((f for f in fragments if f.engine == "scip" and f.status == "ok"), None)
    if scip is not None:
        covered = {n.source_file for n in scip.nodes.values() if n.source_file}
        scip_keys = {e.key for e in scip.edges if e.relation in STRUCTURAL}
        for key, edge in out.edges.items():
            if edge.relation not in STRUCTURAL or edge.file not in covered or edge.target.startswith("lib:"):
                continue  # SCIP never emits edges to external libraries, so those are not disagreements
            engines = {engine_of(p) for p in edge.provenance}
            if "scip" in engines:
                if engines & {"graphify", "ast"}:
                    out.disagreements["agree"] += 1
                else:
                    out.disagreements["scip_only"] += 1
            elif key not in scip_keys:
                out.disagreements["graphify_only"] += 1
                if len(out.disagreement_samples) < 20:
                    out.disagreement_samples.append(
                        {
                            "source": edge.source,
                            "target": edge.target,
                            "relation": edge.relation,
                            "file": edge.file or "",
                        }
                    )
    return out
