"""In-memory GraphStore with the same semantics as the Postgres store. Used by unit tests and exports."""

from collections import deque
from collections.abc import Iterable
from typing import Any

from hapm_sentinel.model import Edge, Node
from hapm_sentinel.store import allowed_confidences


class MemoryGraphStore:
    def __init__(
        self, nodes: Iterable[Node], edges: Iterable[Edge], line_counts: dict[tuple[str, str, str], int] | None = None
    ) -> None:
        self._nodes: dict[str, Node] = {}
        for n in nodes:
            self._nodes.setdefault(n.id, n)
        self._edges = list(edges)
        self._lines = line_counts or {}

    def find(
        self, needle: str, repos: list[str] | None, types: list[str] | None, limit: int, include_hidden: bool = False
    ) -> list[Node]:
        low = needle.lower()
        hits = [
            n
            for n in self._nodes.values()
            if (low in n.label.lower() or low in n.id.lower())
            and (not repos or n.repo in repos)
            and (not types or n.type in types)
            and (include_hidden or n.in_default)
        ]
        exact = [
            n for n in hits if n.label.lower() == low or n.id.lower() == low or n.label.lower().endswith("." + low)
        ]
        return sorted(exact or hits, key=lambda n: n.id)[:limit]

    def nodes(self, ids: Any) -> dict[str, Node]:
        return {i: self._nodes[i] for i in ids if i in self._nodes}

    def neighbors(self, ids: list[str], relations: list[str] | None, direction: str, min_confidence: str) -> list[Edge]:
        allowed, wanted = set(allowed_confidences(min_confidence)), set(ids)
        out = []
        for e in self._edges:
            if e.confidence not in allowed or (relations and e.relation not in relations):
                continue
            if (direction in ("out", "both") and e.source in wanted) or (
                direction in ("in", "both") and e.target in wanted
            ):
                out.append(e)
        return out

    def impact(self, start: str, relations: list[str], depth: int, min_confidence: str) -> list[tuple[str, int, Edge]]:
        allowed = set(allowed_confidences(min_confidence))
        found: dict[str, tuple[int, Edge]] = {}
        queue = deque([(start, 0)])
        seen = {start}
        while queue:
            node, d = queue.popleft()
            if d >= depth:
                continue
            for e in self._edges:
                if e.target == node and e.relation in relations and e.confidence in allowed and e.source not in seen:
                    seen.add(e.source)
                    found[e.source] = (d + 1, e)
                    queue.append((e.source, d + 1))
        return [(n, d, e) for n, (d, e) in found.items()]

    def path(
        self, start: str, goal: str, relations: list[str] | None, depth: int, min_confidence: str
    ) -> list[Edge] | None:
        allowed = set(allowed_confidences(min_confidence))
        queue: deque[tuple[str, list[Edge]]] = deque([(start, [])])
        seen = {start}
        while queue:
            node, trail = queue.popleft()
            if node == goal:
                return trail
            if len(trail) >= depth:
                continue
            for e in self._edges:
                if e.confidence not in allowed or (relations and e.relation not in relations):
                    continue
                nxt = e.target if e.source == node else (e.source if e.target == node else None)
                if nxt and nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, [*trail, e]))
        return None

    def line_count(self, repo: str, sha: str, path: str) -> int | None:
        return self._lines.get((repo, sha, path))
