"""Postgres storage for the Sentinel graph (migration 0003). Paths and neighbourhoods use recursive CTEs.

The "current" graph is every repository's head SHA (sentinel_heads). Older SHAs are kept up to
`retention_shas` for diffs. Cross-repository links are a separate layer rebuilt on every link run.
"""

import json
from collections.abc import Iterable
from typing import Any

from hapm_sentinel.model import CONFIDENCE_RANK, Edge, Node
from sqlalchemy import Engine, text

CURRENT_NODES = "(SELECT n.* FROM sentinel_nodes n JOIN sentinel_heads h ON h.repo = n.repo AND h.sha = n.sha)"
CURRENT_EDGES = "(SELECT e.* FROM sentinel_edges e JOIN sentinel_heads h ON h.repo = e.repo AND h.sha = e.sha)"


def _node(row: Any) -> Node:
    return Node(
        row.id,
        row.type,
        row.label,
        row.repo,
        row.sha,
        row.source_file,
        row.line_start,
        row.line_end,
        row.tier,
        list(row.provenance or []),
        dict(row.attrs or {}),
        row.summary,
        row.in_default,
        row.layer,
    )


def _edge(row: Any) -> Edge:
    return Edge(
        row.source,
        row.target,
        row.relation,
        row.confidence,
        row.repo,
        row.sha,
        row.file,
        row.line_start,
        row.line_end,
        list(row.provenance or []),
        row.cross_repo,
        dict(row.attrs or {}),
        row.layer,
    )


def allowed_confidences(min_confidence: str) -> list[str]:
    limit = CONFIDENCE_RANK.get(min_confidence, 1)
    return [c for c, r in CONFIDENCE_RANK.items() if r <= limit]


class SentinelStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ---- runs -----------------------------------------------------------------------------------------
    def start_run(self, run_id: str, repo: str, mode: str, fleet_run_id: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel_runs (id, fleet_run_id, repo, status, mode, stage) "
                    "VALUES (:i, :f, :r, 'running', :m, 'started') ON CONFLICT (id) DO NOTHING"
                ),
                {"i": run_id, "f": fleet_run_id, "r": repo, "m": mode},
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, params = [], {"i": run_id}
        for key, value in fields.items():
            if key in ("engines", "stats", "llm"):
                sets.append(f"{key} = CAST(:{key} AS jsonb)")
                params[key] = json.dumps(value, default=str)
            elif key == "finished":
                sets.append("finished_at = now()")
            else:
                sets.append(f"{key} = :{key}")
                params[key] = value
        with self.engine.begin() as conn:
            conn.execute(text(f"UPDATE sentinel_runs SET {', '.join(sets)} WHERE id = :i"), params)  # noqa: S608

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM sentinel_runs WHERE id = :i"), {"i": run_id}).first()
        return dict(row._mapping) if row else None

    def runs(self, repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM sentinel_runs " + ("WHERE repo = :r " if repo else "") + "ORDER BY started_at DESC LIMIT :l"  # noqa: S608 - fixed SQL fragments; values are bound parameters
        )
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(sql), {"r": repo, "l": limit})]

    # ---- heads, caches, facts -------------------------------------------------------------------------
    def heads(self) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            return {r.repo: dict(r._mapping) for r in conn.execute(text("SELECT * FROM sentinel_heads"))}

    def cache_get(self, kind: str, key: str) -> Any:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM sentinel_cache WHERE kind = :k AND key = :y"), {"k": kind, "y": key}
            ).first()
        return row.value if row else None

    def cache_put(self, kind: str, key: str, value: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel_cache (kind, key, value) VALUES (:k, :y, CAST(:v AS jsonb)) "
                    "ON CONFLICT (kind, key) DO UPDATE SET value = EXCLUDED.value, created_at = now()"
                ),
                {"k": kind, "y": key, "v": json.dumps(value, default=str)},
            )

    def cache_clear(self, kind: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM sentinel_cache" + (" WHERE kind = :k" if kind else "")), {"k": kind})  # noqa: S608 - fixed SQL fragments; values are bound parameters

    def facts(self, repo: str, sha: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT facts FROM sentinel_facts WHERE repo = :r AND sha = :s"), {"r": repo, "s": sha}
            ).first()
        return dict(row.facts) if row else None

    # ---- writing a repository graph -------------------------------------------------------------------
    def save_repo(
        self,
        repo: str,
        sha: str,
        run_id: str,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        files: dict[str, tuple[str, int]],
        facts: dict[str, Any],
        ontology_version: str,
        retention: int = 3,
    ) -> None:
        """Replace the repository's graph at this SHA and make it the head, in one transaction."""
        with self.engine.begin() as conn:
            for table in ("sentinel_nodes", "sentinel_edges", "sentinel_files", "sentinel_facts"):
                conn.execute(text(f"DELETE FROM {table} WHERE repo = :r AND sha = :s"), {"r": repo, "s": sha})  # noqa: S608
            node_rows = [
                {
                    "r": repo,
                    "s": sha,
                    "i": n.id,
                    "t": n.type,
                    "l": n.label[:500],
                    "f": n.source_file,
                    "a": n.line_start,
                    "b": n.line_end,
                    "tier": n.tier,
                    "d": n.in_default,
                    "y": n.layer,
                    "u": n.summary,
                    "x": json.dumps(n.attrs, default=str),
                    "p": n.provenance,
                    "o": ontology_version,
                }
                for n in nodes
            ]
            if node_rows:
                conn.execute(
                    text(
                        "INSERT INTO sentinel_nodes (repo, sha, id, type, label, source_file, line_start, line_end, tier, "
                        "in_default, layer, summary, attrs, provenance, ontology_version) VALUES (:r, :s, :i, :t, :l, :f, :a, "
                        ":b, :tier, :d, :y, :u, CAST(:x AS jsonb), :p, :o) ON CONFLICT DO NOTHING"
                    ),
                    node_rows,
                )
            self._insert_edges(conn, list(edges))
            file_rows = [{"r": repo, "s": sha, "p": p, "b": b, "c": c} for p, (b, c) in files.items()]
            if file_rows:
                conn.execute(
                    text(
                        "INSERT INTO sentinel_files (repo, sha, path, blob_sha, line_count) VALUES (:r, :s, :p, :b, :c)"
                    ),
                    file_rows,
                )
            conn.execute(
                text("INSERT INTO sentinel_facts (repo, sha, facts) VALUES (:r, :s, CAST(:f AS jsonb))"),
                {"r": repo, "s": sha, "f": json.dumps(facts, default=str)},
            )
            conn.execute(
                text(
                    "INSERT INTO sentinel_heads (repo, sha, run_id, ontology_version) VALUES (:r, :s, :i, :o) "
                    "ON CONFLICT (repo) DO UPDATE SET sha = EXCLUDED.sha, run_id = EXCLUDED.run_id, "
                    "ontology_version = EXCLUDED.ontology_version, built_at = now()"
                ),
                {"r": repo, "s": sha, "i": run_id, "o": ontology_version},
            )
            # Retention: keep the newest `retention` SHAs that completed for this repository.
            keep = [
                r.sha
                for r in conn.execute(
                    text(
                        "SELECT sha FROM sentinel_runs WHERE repo = :r AND status = 'succeeded' AND sha IS NOT NULL "
                        "GROUP BY sha ORDER BY max(started_at) DESC LIMIT :n"
                    ),
                    {"r": repo, "n": retention},
                )
            ]
            keep.append(sha)
            for table in ("sentinel_nodes", "sentinel_edges", "sentinel_files", "sentinel_facts"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE repo = :r AND NOT (sha = ANY(:k))"),  # noqa: S608
                    {"r": repo, "k": keep},
                )

    def _insert_edges(self, conn: Any, edges: list[Edge]) -> None:
        rows = [
            {
                "r": e.repo,
                "s": e.sha,
                "a": e.source,
                "b": e.target,
                "rel": e.relation,
                "c": e.confidence,
                "f": e.file,
                "ls": e.line_start,
                "le": e.line_end,
                "p": e.provenance,
                "x": e.cross_repo,
                "y": e.layer,
                "t": json.dumps(e.attrs, default=str),
            }
            for e in edges
        ]
        if rows:
            conn.execute(
                text(
                    "INSERT INTO sentinel_edges (repo, sha, source, target, relation, confidence, file, line_start, line_end, "
                    "provenance, cross_repo, layer, attrs) VALUES (:r, :s, :a, :b, :rel, :c, :f, :ls, :le, :p, :x, :y, "
                    "CAST(:t AS jsonb))"
                ),
                rows,
            )

    def replace_links(self, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        """Swap the cross-repository layer for the current heads in one transaction."""
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM sentinel_edges WHERE layer = 'link'"))
            conn.execute(text("DELETE FROM sentinel_nodes WHERE layer = 'link'"))
            rows = [
                {
                    "r": n.repo,
                    "s": n.sha,
                    "i": n.id,
                    "t": n.type,
                    "l": n.label,
                    "tier": n.tier,
                    "x": json.dumps(n.attrs),
                    "p": n.provenance,
                }
                for n in nodes
            ]
            if rows:
                conn.execute(
                    text(
                        "INSERT INTO sentinel_nodes (repo, sha, id, type, label, tier, layer, attrs, provenance, ontology_version) "
                        "VALUES (:r, :s, :i, :t, :l, :tier, 'link', CAST(:x AS jsonb), :p, '1') ON CONFLICT DO NOTHING"
                    ),
                    rows,
                )
            self._insert_edges(conn, list(edges))

    def set_summary(self, repo: str, sha: str, node_id: str, summary: str | None, attrs: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sentinel_nodes SET summary = :u, attrs = attrs || CAST(:x AS jsonb) "
                    "WHERE repo = :r AND sha = :s AND id = :i"
                ),
                {"u": summary, "x": json.dumps(attrs, default=str), "r": repo, "s": sha, "i": node_id},
            )

    def put_embedding(self, repo: str, sha: str, node_id: str, model: str, vector: list[float]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel_embeddings (repo, sha, node_id, model, dimension, embedding) "
                    "VALUES (:r, :s, :i, :m, :d, CAST(:e AS vector)) ON CONFLICT (repo, sha, node_id, model) "
                    "DO UPDATE SET embedding = EXCLUDED.embedding, dimension = EXCLUDED.dimension"
                ),
                {
                    "r": repo,
                    "s": sha,
                    "i": node_id,
                    "m": model,
                    "d": len(vector),
                    "e": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                },
            )

    # ---- reading the current graph --------------------------------------------------------------------
    def repo_nodes(self, repo: str, sha: str, layer: str = "repo") -> dict[str, Node]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM sentinel_nodes WHERE repo = :r AND sha = :s AND layer = :y"),
                {"r": repo, "s": sha, "y": layer},
            ).all()
        return {r.id: _node(r) for r in rows}

    def repo_edges(self, repo: str, sha: str, layer: str | None = None) -> list[Edge]:
        sql = "SELECT * FROM sentinel_edges WHERE repo = :r AND sha = :s" + (" AND layer = :y" if layer else "")  # noqa: S608 - fixed SQL fragments; values are bound parameters
        with self.engine.connect() as conn:
            return [_edge(r) for r in conn.execute(text(sql), {"r": repo, "s": sha, "y": layer})]

    def find(
        self, needle: str, repos: list[str] | None, types: list[str] | None, limit: int, include_hidden: bool = False
    ) -> list[Node]:
        sql = (
            f"SELECT DISTINCT ON (id) * FROM {CURRENT_NODES} n WHERE (lower(label) = lower(:q) OR lower(id) = lower(:q) "  # noqa: S608
            "OR lower(label) LIKE lower(:like) OR lower(id) LIKE lower(:like))"
            + (" AND repo = ANY(:repos)" if repos else "")
            + (" AND type = ANY(:types)" if types else "")
            + ("" if include_hidden else " AND in_default")
            + " ORDER BY id, layer LIMIT :l"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {"q": needle, "like": f"%{needle}%", "repos": repos, "types": types, "l": limit * 5}
            ).all()
        nodes = [_node(r) for r in rows]
        exact = [
            n
            for n in nodes
            if n.label.lower() == needle.lower()
            or n.id.lower() == needle.lower()
            or n.label.lower().endswith("." + needle.lower())
        ]
        return (exact or nodes)[:limit]

    def nodes(self, ids: Iterable[str]) -> dict[str, Node]:
        ids = list(set(ids))
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT DISTINCT ON (id) * FROM {CURRENT_NODES} n WHERE id = ANY(:ids) ORDER BY id, layer"),  # noqa: S608
                {"ids": ids},
            ).all()
        return {r.id: _node(r) for r in rows}

    def neighbors(self, ids: list[str], relations: list[str] | None, direction: str, min_confidence: str) -> list[Edge]:
        clauses = []
        if direction in ("out", "both"):
            clauses.append("source = ANY(:ids)")
        if direction in ("in", "both"):
            clauses.append("target = ANY(:ids)")
        sql = (
            f"SELECT * FROM {CURRENT_EDGES} e WHERE ({' OR '.join(clauses)}) AND confidence = ANY(:c)"  # noqa: S608
            + (" AND relation = ANY(:rel)" if relations else "")
        )
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {"ids": ids, "c": allowed_confidences(min_confidence), "rel": relations}
            ).all()
        return [_edge(r) for r in rows]

    def impact(self, start: str, relations: list[str], depth: int, min_confidence: str) -> list[tuple[str, int, Edge]]:
        """Everything that depends on `start`: walk edges backwards (who calls, imports, consumes, subscribes)."""
        sql = f"""
            WITH RECURSIVE walk(node, depth, edge_id, trail) AS (
              SELECT CAST(:start AS text), 0, CAST(NULL AS bigint), ARRAY[CAST(:start AS text)]
              UNION ALL
              SELECT e.source, w.depth + 1, e.id, w.trail || e.source
              FROM walk w JOIN {CURRENT_EDGES} e ON e.target = w.node
              WHERE w.depth < :depth AND e.relation = ANY(:rel) AND e.confidence = ANY(:c) AND NOT e.source = ANY(w.trail)
            )
            SELECT DISTINCT ON (w.node) w.node, w.depth, e.*
            FROM walk w JOIN sentinel_edges e ON e.id = w.edge_id
            ORDER BY w.node, w.depth
        """  # noqa: S608
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {"start": start, "depth": depth, "rel": relations, "c": allowed_confidences(min_confidence)}
            ).all()
        return [(r.node, r.depth, _edge(r)) for r in rows]

    def path(
        self, start: str, goal: str, relations: list[str] | None, depth: int, min_confidence: str
    ) -> list[Edge] | None:
        """Shortest undirected path (edges keep their own direction) between two nodes, up to `depth` hops."""
        rel_filter = "AND e.relation = ANY(:rel)" if relations else ""
        sql = f"""
            WITH RECURSIVE walk(node, depth, trail, edges) AS (
              SELECT CAST(:start AS text), 0, ARRAY[CAST(:start AS text)], ARRAY[]::bigint[]
              UNION ALL
              SELECT CASE WHEN e.source = w.node THEN e.target ELSE e.source END, w.depth + 1,
                     w.trail || CASE WHEN e.source = w.node THEN e.target ELSE e.source END, w.edges || e.id
              FROM walk w JOIN {CURRENT_EDGES} e ON (e.source = w.node OR e.target = w.node)
              WHERE w.depth < :depth AND e.confidence = ANY(:c) {rel_filter}
                AND NOT (CASE WHEN e.source = w.node THEN e.target ELSE e.source END) = ANY(w.trail)
                AND w.node <> :goal
            )
            SELECT edges FROM walk WHERE node = :goal ORDER BY depth LIMIT 1
        """  # noqa: S608
        with self.engine.connect() as conn:
            row = conn.execute(
                text(sql),
                {
                    "start": start,
                    "goal": goal,
                    "depth": depth,
                    "rel": relations,
                    "c": allowed_confidences(min_confidence),
                },
            ).first()
            if row is None:
                return None
            ids = list(row.edges)
            edges = {
                r.id: _edge(r)
                for r in conn.execute(text("SELECT * FROM sentinel_edges WHERE id = ANY(:i)"), {"i": ids})
            }
        return [edges[i] for i in ids]

    def current_graph(self, repos: list[str] | None = None) -> tuple[dict[str, Node], list[Edge]]:
        """The whole current graph (deduplicated nodes, every edge), optionally for some repositories."""
        where = " WHERE repo = ANY(:r)" if repos else ""
        with self.engine.connect() as conn:
            nodes = {
                r.id: _node(r)
                for r in conn.execute(
                    text(f"SELECT DISTINCT ON (id) * FROM {CURRENT_NODES} n{where} ORDER BY id, layer"),  # noqa: S608
                    {"r": repos},
                )
            }
            edges = [_edge(r) for r in conn.execute(text(f"SELECT * FROM {CURRENT_EDGES} e{where}"), {"r": repos})]  # noqa: S608
        return nodes, edges

    def line_counts(self) -> dict[tuple[str, str, str], int]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT f.repo, f.sha, f.path, f.line_count FROM sentinel_files f JOIN sentinel_heads h ON h.repo = f.repo AND h.sha = f.sha"
                )
            ).all()
        return {(r.repo, r.sha, r.path): r.line_count for r in rows}

    def line_count(self, repo: str, sha: str, path: str) -> int | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT line_count FROM sentinel_files WHERE repo = :r AND sha = :s AND path = :p"),
                {"r": repo, "s": sha, "p": path},
            ).first()
        return int(row.line_count) if row else None

    def stats(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            nodes = conn.execute(
                text(f"SELECT repo, type, count(*) AS c FROM {CURRENT_NODES} n GROUP BY repo, type")  # noqa: S608 - fixed SQL fragments; values are bound parameters
            ).all()  # noqa: S608
            edges = conn.execute(
                text(
                    f"SELECT repo, relation, confidence, cross_repo, count(*) AS c FROM {CURRENT_EDGES} e GROUP BY 1, 2, 3, 4"  # noqa: S608 - fixed SQL fragments; values are bound parameters
                )  # noqa: S608
            ).all()
            heads = conn.execute(text("SELECT repo, sha, built_at FROM sentinel_heads")).all()
        out: dict[str, Any] = {"repos": {}}
        for h in heads:
            out["repos"][h.repo] = {
                "sha": h.sha,
                "built_at": h.built_at.isoformat(),
                "nodes": {},
                "edges": {},
                "confidence": {},
                "cross_repo_edges": 0,
            }
        for n in nodes:
            out["repos"].setdefault(n.repo, {"nodes": {}, "edges": {}, "confidence": {}, "cross_repo_edges": 0})[
                "nodes"
            ][n.type] = n.c
        for e in edges:
            r = out["repos"].setdefault(e.repo, {"nodes": {}, "edges": {}, "confidence": {}, "cross_repo_edges": 0})
            r["edges"][e.relation] = r["edges"].get(e.relation, 0) + e.c
            r["confidence"][e.confidence] = r["confidence"].get(e.confidence, 0) + e.c
            if e.cross_repo:
                r["cross_repo_edges"] += e.c
        return out
