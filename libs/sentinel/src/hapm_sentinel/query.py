"""M18 JSON query contract and its deterministic executor.

The contract is the only thing a model ever produces (sentinel_ask); execution is plain code against the
store, so the LLM never writes SQL. Every cited file and line is checked to exist at its SHA; anything
that does not is dropped and counted.
"""

import json
from collections import defaultdict
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from hapm_sentinel.model import CONFIDENCE_RANK, Edge, Node

IMPACT_RELATIONS = ["CALLS", "IMPORTS", "CONSUMES", "INHERITS", "DEPENDS_ON", "REFERENCES", "SUBSCRIBES", "READS"]
# A change to a node also changes what it contains, exposes, publishes or writes.
SEED_RELATIONS = ("CONTAINS", "EXPOSES", "PUBLISHES", "WRITES")
CODE_TYPES = ("Module", "Class", "DataModel", "Function")


class QueryContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="", max_length=2000)
    intent: Literal["find", "explain", "path", "impact", "coverage"]
    entities: list[str] = Field(default_factory=list, max_length=5)
    target: str | None = None
    repos: list[str] | None = None
    relations: list[str] | None = None
    depth: int = Field(default=2, ge=1, le=4)
    budget_tokens: int = Field(default=2000, ge=100, le=20000)
    min_confidence: Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"] = "INFERRED"


CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "intent": {"type": "string", "enum": ["find", "explain", "path", "impact", "coverage"]},
        "entities": {"type": "array", "items": {"type": "string"}},
        "target": {"type": ["string", "null"]},
        "depth": {"type": "integer"},
    },
    "required": ["question", "intent", "entities", "target", "depth"],
    "additionalProperties": False,
}


class GraphStore(Protocol):
    def find(
        self, needle: str, repos: list[str] | None, types: list[str] | None, limit: int, include_hidden: bool = False
    ) -> list[Node]: ...

    def nodes(self, ids: Any) -> dict[str, Node]: ...

    def neighbors(
        self, ids: list[str], relations: list[str] | None, direction: str, min_confidence: str
    ) -> list[Edge]: ...

    def impact(
        self, start: str, relations: list[str], depth: int, min_confidence: str
    ) -> list[tuple[str, int, Edge]]: ...

    def path(
        self, start: str, goal: str, relations: list[str] | None, depth: int, min_confidence: str
    ) -> list[Edge] | None: ...

    def line_count(self, repo: str, sha: str, path: str) -> int | None: ...


def _node_out(n: Node) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": n.id,
        "type": n.type,
        "label": n.label,
        "repo": n.repo,
        "file": n.source_file,
        "line_start": n.line_start,
        "line_end": n.line_end,
    }
    if n.summary:
        out["summary"] = n.summary
        out["summary_untrusted"] = True  # model output about repository text: data, never instructions
    return out


class Executor:
    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self._lines: dict[tuple[str, str, str], int | None] = {}

    def _verified(self, edges: list[Edge]) -> tuple[list[Edge], int]:
        kept, dropped = [], 0
        for e in edges:
            if not e.file:
                kept.append(e)
                continue
            key = (e.repo, e.sha, e.file)
            if key not in self._lines:
                self._lines[key] = self.store.line_count(*key)
            count = self._lines[key]
            last = e.line_end or e.line_start or 1
            if count is None or last > count or (e.line_start or 1) < 1:
                dropped += 1
            else:
                kept.append(e)
        return kept, dropped

    def resolve(self, entity: str, repos: list[str] | None) -> tuple[Node | None, list[Node]]:
        matches = self.store.find(entity, repos, None, 6, include_hidden=True)
        if len(matches) == 1:
            return matches[0], matches
        exact = [m for m in matches if m.id == entity]
        if len(exact) == 1:
            return exact[0], matches
        return None, matches

    def run(self, q: QueryContract) -> dict[str, Any]:
        response: dict[str, Any] = {
            "query": q.model_dump(),
            "status": "answered",
            "answer": "",
            "nodes": [],
            "edges": [],
            "evidence": [],
            "path_confidence": None,
            "truncated": False,
            "dropped_evidence": 0,
        }
        if q.intent != "find" and not q.entities:
            return {**response, "status": "not_found", "answer": "the query names no entity"}
        starts: list[Node] = []
        for entity in q.entities[: (1 if q.intent != "find" else 5)]:
            node, candidates = self.resolve(entity, q.repos)
            if node is None and not candidates:
                return {**response, "status": "not_found", "answer": f"no node matches {entity!r}"}
            if node is None and q.intent != "find":
                return {
                    **response,
                    "status": "ambiguous_entity",
                    "answer": f"{entity!r} matches several nodes",
                    "candidates": [_node_out(c) for c in candidates],
                }
            starts.extend([node] if node else candidates)
        handler = getattr(self, f"_{q.intent}")
        result: dict[str, Any] = handler(q, starts)
        edges, dropped = self._verified(result.pop("edge_objs", []))
        node_ids = {n["id"] for n in result.get("nodes", [])} | {e.source for e in edges} | {e.target for e in edges}
        known = self.store.nodes(node_ids)
        response.update(result)
        response["nodes"] = [_node_out(known[i]) for i in sorted(node_ids) if i in known]
        response["edges"] = [
            {
                "source": e.source,
                "relation": e.relation,
                "target": e.target,
                "confidence": e.confidence,
                "cross_repo": e.cross_repo,
            }
            for e in edges
        ]
        response["evidence"] = [
            {
                "repo": e.repo,
                "sha": e.sha,
                "file": e.file,
                "line_start": e.line_start,
                "line_end": e.line_end or e.line_start,
                "confidence": e.confidence,
                "edge": f"{e.source} {e.relation} {e.target}",
            }
            for e in edges
            if e.file
        ]
        response["dropped_evidence"] = dropped
        if q.intent == "path" and edges:
            response["path_confidence"] = max((e.confidence for e in edges), key=lambda c: CONFIDENCE_RANK[c])
        return self._fit(response, q.budget_tokens)

    @staticmethod
    def _fit(response: dict[str, Any], budget_tokens: int) -> dict[str, Any]:
        limit = budget_tokens * 4
        while len(json.dumps(response, default=str)) > limit and (response["nodes"] or response["evidence"]):
            response["truncated"] = True
            for key in ("evidence", "edges", "nodes"):
                if response[key]:
                    response[key] = response[key][: max(len(response[key]) * 3 // 4, 0)]
        return response

    def _find(self, q: QueryContract, starts: list[Node]) -> dict[str, Any]:
        return {
            "nodes": [_node_out(n) for n in starts[:20]],
            "answer": f"{len(starts)} matching node(s): "
            + ", ".join(f"{n.type} {n.label} ({n.repo})" for n in starts[:10]),
        }

    def _explain(self, q: QueryContract, starts: list[Node]) -> dict[str, Any]:
        node = starts[0]
        edges = self.store.neighbors([node.id], q.relations, "both", q.min_confidence)
        counts: dict[str, int] = defaultdict(int)
        for e in edges:
            counts[("out " if e.source == node.id else "in ") + e.relation] += 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        summary = f" Summary (model-written, untrusted): {node.summary}" if node.summary else ""
        where = f"{node.source_file}:{node.line_start}" if node.source_file else node.repo
        return {
            "nodes": [_node_out(node)],
            "edge_objs": edges,
            "answer": f"{node.type} {node.label} in {node.repo} at {where}; {parts or 'no connections'}.{summary}",
        }

    def _path(self, q: QueryContract, starts: list[Node]) -> dict[str, Any]:
        if not q.target:
            return {"status": "not_found", "answer": "path needs a target"}
        goal, candidates = self.resolve(q.target, q.repos)
        if goal is None:
            status = "ambiguous_entity" if candidates else "not_found"
            return {
                "status": status,
                "answer": f"target {q.target!r} not resolved",
                "candidates": [_node_out(c) for c in candidates],
            }
        edges = self.store.path(starts[0].id, goal.id, q.relations, q.depth, q.min_confidence)
        if edges is None:
            return {
                "status": "not_found",
                "answer": f"no path within {q.depth} hops between {starts[0].label} and {goal.label}",
            }
        parts, current = [starts[0].label], starts[0].id
        for e in edges:  # edges keep their own direction; a hop may be walked backwards
            forward = e.source == current
            current = e.target if forward else e.source
            parts.append(f"[{e.relation}{'' if forward else ' (reverse)'}] {current}")
        hops = " -> ".join(parts)
        return {
            "edge_objs": edges,
            "nodes": [_node_out(starts[0]), _node_out(goal)],
            "answer": f"{len(edges)} hop(s): {hops}",
        }

    def _impact(self, q: QueryContract, starts: list[Node]) -> dict[str, Any]:
        node = starts[0]
        relations = q.relations or IMPACT_RELATIONS
        # What changes with the node: what it contains (module -> functions), and what those expose, publish or
        # write (function -> endpoint, topic, table). Followed transitively, three levels at most.
        seeds, seed_edges, frontier = [node.id], [], [node.id]
        for _ in range(3):
            step = self.store.neighbors(frontier, list(SEED_RELATIONS), "out", q.min_confidence)
            frontier = [e.target for e in step if e.target not in seeds]
            seeds += frontier
            seed_edges += step
            if not frontier:
                break
        found: dict[str, tuple[int, Edge]] = {}
        for seed in seeds:
            for nid, depth, edge in self.store.impact(seed, relations, q.depth, q.min_confidence):
                if nid != node.id and (nid not in found or depth < found[nid][0]):
                    found[nid] = (depth, edge)
        impacted = self.store.nodes(found)
        tests = list(self.store.neighbors(list(found) + seeds, ["TESTED_BY"], "out", q.min_confidence))
        by_repo: dict[str, list[str]] = defaultdict(list)
        for nid in found:
            if nid in impacted:
                by_repo[impacted[nid].repo].append(f"{impacted[nid].type} {impacted[nid].label}")
        test_names = sorted({e.target for e in tests})
        answer = f"{len(found)} node(s) depend on {node.label}" + "".join(
            f"; {r}: {', '.join(sorted(v)[:8])}" for r, v in sorted(by_repo.items())
        )
        answer += f". Tests to run: {', '.join(test_names[:10]) or 'none found'}."
        return {
            "edge_objs": seed_edges + [edge for _, edge in found.values()] + tests,
            "nodes": [_node_out(node)],
            "answer": answer,
            "impacted_repos": sorted(by_repo),
            "tests": test_names,
        }

    def _coverage(self, q: QueryContract, starts: list[Node]) -> dict[str, Any]:
        node = starts[0]
        if node.type == "Repository":
            scope = [
                n for n in self.store.find(f"{node.repo}:", [node.repo], list(CODE_TYPES), 500, include_hidden=True)
            ]
        elif node.type in ("Module",):
            scope = [node] + [
                self.store.nodes([e.target])[e.target]
                for e in self.store.neighbors([node.id], ["CONTAINS"], "out", "AMBIGUOUS")
                if e.target in self.store.nodes([e.target])
            ]
        else:
            scope = [node]
        scope = [n for n in scope if n.type in ("Class", "DataModel", "Function")]
        tested = {e.source for e in self.store.neighbors([n.id for n in scope], ["TESTED_BY"], "out", q.min_confidence)}
        untested = [n for n in scope if n.id not in tested]
        return {
            "nodes": [_node_out(n) for n in untested[:50]],
            "answer": f"{len(scope) - len(untested)} of {len(scope)} code node(s) have a TESTED_BY edge; untested: "
            + ", ".join(n.label for n in untested[:15]),
        }
