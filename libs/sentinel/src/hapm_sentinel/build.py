"""Build one repository's graph, link the fleet, and plan builds (dry run).

Per repository, at a pinned SHA:
  snapshot -> facts -> engines (graphify + SCIP, else graphify only, else Python ast, else failed)
  -> merge with precedence -> architecture edges -> ontology conformance -> tiers and node budget -> store.
Incremental by default: a repository whose head SHA and ontology version are unchanged is skipped, and
engine outputs are cached per SHA. `force` ignores every cache.
"""

import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from hapm_codeintel.snapshot import Snapshot, blob_sha
from hapm_config import ProjectConfig, RepoConfig

from hapm_sentinel.engines import ast_py
from hapm_sentinel.engines.graphify import GraphifySchemaError, map_graph
from hapm_sentinel.engines.runner import EngineRunner
from hapm_sentinel.engines.scip import map_index
from hapm_sentinel.enrich import enrich_repo
from hapm_sentinel.facts import RepoFacts, repo_facts
from hapm_sentinel.linkers import RepoView, cross_repo, derive_tests, intra_repo
from hapm_sentinel.llm import GatewayLLM, effective_llm
from hapm_sentinel.merge import merge
from hapm_sentinel.model import Edge, Fragment, Node
from hapm_sentinel.ontology import Ontology
from hapm_sentinel.sources import SourceReader
from hapm_sentinel.store import SentinelStore

LANGUAGE_SUFFIXES = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx"),
    "java": (".java",),
    "go": (".go",),
}


@dataclass
class BuildContext:
    project: ProjectConfig
    store: SentinelStore
    reader: SourceReader
    runner: EngineRunner
    ontology: Ontology


def new_run_id(prefix: str = "snl") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def code_languages(snapshot: Snapshot) -> list[str]:
    return sorted(
        lang for lang, suffixes in LANGUAGE_SUFFIXES.items() if any(p.endswith(suffixes) for p in snapshot.files)
    )


def _project_identity(facts: RepoFacts, repo: RepoConfig) -> tuple[str, str]:
    for m in facts.manifests:
        return m["name"], m.get("version") or "0"
    return repo.name, "0"


def run_engines(
    ctx: BuildContext, repo: RepoConfig, snap: Snapshot, facts: RepoFacts, force: bool
) -> tuple[list[Fragment], dict[str, Any], str]:
    cfg = ctx.project.sentinel.engines
    status: dict[str, Any] = {}
    fragments: list[Fragment] = []
    graphify_ok = False
    if repo.sentinel.engines.graphify and cfg.graphify.enabled:
        key = f"{repo.name}:{snap.sha}:{cfg.graphify.version}"
        cached = None if force else ctx.store.cache_get("graphify", key)
        started = time.monotonic()
        out_status: str | None = "ok" if cached else None
        data: Any = cached
        error: str | None = None
        if cached is None:
            out = ctx.runner.graphify(repo, snap)
            out_status, data, error = out.status, out.data, out.error
        status["graphify"] = {"version": cfg.graphify.version, "status": out_status, "cached": cached is not None}
        if out_status == "ok" and data is not None:
            try:
                frag = map_graph(data, snap, cfg.graphify.version, ctx.project.sentinel.relation_map)
                fragments.append(frag)
                graphify_ok = True
                if cached is None:
                    ctx.store.cache_put("graphify", key, data)
            except GraphifySchemaError as exc:
                status["graphify"].update(status="failed", error=f"schema changed: {exc}")
        elif error:
            status["graphify"]["error"] = error
        status["graphify"]["duration_ms"] = int((time.monotonic() - started) * 1000)
    else:
        status["graphify"] = {"status": "off"}

    languages = code_languages(snap)
    precise: list[str] = []
    if repo.sentinel.engines.scip == "auto" and cfg.scip.enabled:
        project = _project_identity(facts, repo)
        for language in languages:
            if language not in cfg.scip.indexers:
                status[f"scip:{language}"] = {"status": "unavailable", "error": "no indexer configured"}
                continue
            indexer = cfg.scip.indexers[language]
            key = f"{repo.name}:{snap.sha}:{indexer.name}@{indexer.version}"
            cached = None if force else ctx.store.cache_get("scip", key)
            started = time.monotonic()
            out_status = None
            error = None
            if cached is not None:
                out_status, data, error = "ok", cached, None
            else:
                out = ctx.runner.scip(repo, snap, language, project)
                out_status, data, error = out.status, out.data, out.error
            entry: dict[str, Any] = {
                "indexer": f"{indexer.name}@{indexer.version}",
                "status": out_status,
                "cached": cached is not None,
            }
            if out_status == "ok" and data is not None:
                frag = map_index(data, snap, indexer.name)
                if frag.nodes:
                    fragments.append(frag)
                    precise.append(language)
                    if cached is None:
                        ctx.store.cache_put("scip", key, data)
                else:
                    entry.update(status="failed", error="index defined no symbols")
            elif error:
                entry["error"] = error[:300]
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
            status[f"scip:{language}"] = entry
    else:
        status["scip"] = {"status": "off"}

    if not graphify_ok:
        frag = ast_py.extract(snap)
        status["ast"] = {"status": "ok" if frag.nodes else "failed", "version": ast_py.VERSION}
        if frag.nodes:
            fragments.append(frag)
    if not fragments:
        raise RuntimeError(
            "every engine failed: " + "; ".join(f"{k}: {v.get('error', v.get('status'))}" for k, v in status.items())
        )
    if graphify_ok and languages and set(languages) <= set(precise):
        tier = "exact"
    elif graphify_ok and precise:
        tier = "mixed"
    elif graphify_ok:
        tier = "approximate"
    else:
        tier = "fallback"
    return fragments, status, tier


def apply_budget(
    nodes: dict[str, Node], edges: list[Edge], repo: RepoConfig
) -> tuple[dict[str, Node], list[Edge], dict[str, Any]]:
    allowed = set(repo.sentinel.tiers) | {1}
    dropped = [n for n in nodes.values() if n.tier not in allowed]
    for n in dropped:
        del nodes[n.id]
    edges = [e for e in edges if e.source in nodes and e.target in nodes]
    hidden = 0
    if len(nodes) > repo.sentinel.node_budget:
        for n in nodes.values():
            if n.tier == 3:
                n.in_default = False
                hidden += 1
    return (
        nodes,
        edges,
        {"dropped_by_tier": len(dropped), "hidden_over_budget": hidden, "budget": repo.sentinel.node_budget},
    )


def build_repo(
    ctx: BuildContext,
    name: str,
    *,
    force: bool = False,
    fleet_run_id: str | None = None,
    run_id: str | None = None,
    sha: str | None = None,
) -> dict[str, Any]:
    repo = ctx.project.repo(name)
    run_id = run_id or new_run_id()
    store = ctx.store
    store.start_run(run_id, name, "force" if force else "incremental", fleet_run_id)
    started = time.monotonic()
    try:
        source = ctx.reader.read(repo, sha)
        snap = source.snapshot
        store.update_run(run_id, sha=snap.sha, stage="snapshot")
        head = store.heads().get(name)
        if not force and head and head["sha"] == snap.sha and head["ontology_version"] == ctx.ontology.version:
            store.update_run(
                run_id, status="unchanged", stage="done", finished=True, stats={"reason": "head SHA unchanged"}
            )
            return {"repo": name, "run_id": run_id, "sha": snap.sha, "status": "unchanged"}
        facts = repo_facts(snap.files)
        fragments, engines, tier = run_engines(ctx, repo, snap, facts, force)
        store.update_run(run_id, stage="engines", engines=engines, precision_tier=tier)
        merged = merge(fragments)
        nodes = merged.nodes
        scip = [f for f in fragments if f.engine == "scip"]
        view = RepoView(
            name,
            snap.sha,
            repo,
            nodes,
            facts,
            source.commits,
            {k: v for f in scip for k, v in f.exports.items()},
            [r for f in scip for r in f.external_refs],
            next((f.package for f in scip if f.package), ""),
        )
        extra_nodes, extra_edges = intra_repo(view)
        for node in extra_nodes.values():
            nodes.setdefault(node.id, node)
        all_edges = merged.edge_list() + extra_edges
        all_edges += derive_tests(view, all_edges)
        edges, rejected = ctx.ontology.conform(nodes, all_edges)
        for frag in fragments:
            rejected.update({f"{frag.engine}: {k}": v for k, v in frag.rejected.items()})
        edges = list({(e.source, e.target, e.relation): e for e in edges}.values())
        nodes, edges, budget = apply_budget(nodes, edges, repo)
        # An empty file still has line 1 (the position editors and evidence point at).
        files = {p: (blob_sha(t), max(len(t.splitlines()), 1)) for p, t in snap.files.items()}
        facts_payload = {
            "facts": facts.as_dict(),
            "exports": view.exports,
            "external_refs": view.external_refs,
            "package": view.package,
            "commits": source.commits,
            "skipped": snap.skipped,
        }
        store.save_repo(
            name,
            snap.sha,
            run_id,
            nodes.values(),
            edges,
            files,
            facts_payload,
            ctx.ontology.version,
            ctx.project.sentinel.retention_shas,
        )
        stats = {
            "nodes": len(nodes),
            "edges": len(edges),
            "node_types": dict(Counter(n.type for n in nodes.values())),
            "relations": dict(Counter(e.relation for e in edges)),
            "confidence": dict(Counter(e.confidence for e in edges)),
            "rejected": dict(rejected),
            "disagreements": dict(merged.disagreements),
            "disagreement_samples": merged.disagreement_samples,
            "budget": budget,
            "files": len(snap.files),
            "skipped_files": len(snap.skipped),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        store.update_run(run_id, stage="stored", stats=stats)
        mode = effective_llm(ctx.project, repo)
        llm_stats: dict[str, Any] = {"mode": mode}
        if mode != "off" and repo.sentinel.enrich:
            store.update_run(run_id, stage="enrich")
            enriched = enrich_repo(ctx.project, repo, store, snap, nodes, edges, GatewayLLM(run_id=run_id))
            llm_stats.update(enriched.as_dict())
        store.update_run(run_id, status="succeeded", stage="done", llm=llm_stats, finished=True)
        return {
            "repo": name,
            "run_id": run_id,
            "sha": snap.sha,
            "status": "succeeded",
            "precision_tier": tier,
            "engines": engines,
            "llm": llm_stats,
            **stats,
        }
    except Exception as exc:
        store.update_run(run_id, status="failed", stage="failed", error=str(exc)[:1000], finished=True)
        return {"repo": name, "run_id": run_id, "status": "failed", "error": str(exc)}


def link_fleet(ctx: BuildContext) -> dict[str, Any]:
    """Rebuild the cross-repository layer from every repository's current head."""
    views: dict[str, RepoView] = {}
    for name, head in ctx.store.heads().items():
        if name not in ctx.project.repos:
            continue
        payload = ctx.store.facts(name, head["sha"]) or {}
        views[name] = RepoView(
            name,
            head["sha"],
            ctx.project.repos[name],
            ctx.store.repo_nodes(name, head["sha"]),
            RepoFacts.from_dict(payload.get("facts", {})),
            payload.get("commits", []),
            payload.get("exports", {}),
            payload.get("external_refs", []),
            payload.get("package", ""),
        )
    result = cross_repo(views, ctx.project.sentinel)
    types = {n.id: n.type for v in views.values() for n in v.nodes.values()}
    kept: list[Edge] = []
    rejected: Counter[str] = Counter()
    for edge in result.edges:
        problem = ctx.ontology.edge_problem(edge, types)
        if problem:
            rejected[problem] += 1
        else:
            kept.append(edge)
    ctx.store.replace_links([], kept)
    report = {
        "repos": result.report,
        "edges": len(kept),
        "confidence": dict(Counter(e.confidence for e in kept)),
        "relations": dict(Counter(e.relation for e in kept)),
        "rejected": dict(rejected),
    }
    ctx.store.cache_put("link_report", "latest", report)
    return report


def enabled_repos(project: ProjectConfig, only: list[str] | None = None) -> list[str]:
    if only:
        for name in only:
            project.repo(name)
        return only
    return sorted(n for n, r in project.repos.items() if r.sentinel.enabled)


def build_fleet(
    ctx: BuildContext, repos: list[str] | None = None, *, force: bool = False, fleet_run_id: str | None = None
) -> dict[str, Any]:
    fleet_run_id = fleet_run_id or new_run_id("fleet")
    names = enabled_repos(ctx.project, repos)
    with ThreadPoolExecutor(max_workers=ctx.project.sentinel.concurrency) as pool:
        results = list(pool.map(lambda n: build_repo(ctx, n, force=force, fleet_run_id=fleet_run_id), names))
    links = link_fleet(ctx) if any(r["status"] in ("succeeded", "unchanged") for r in results) else {}
    return {"fleet_run_id": fleet_run_id, "repos": results, "links": links}


def plan_fleet(
    ctx: BuildContext, repos: list[str] | None = None, force: bool = False, tokens_per_node: int = 900
) -> list[dict[str, Any]]:
    """Dry run: what would be rebuilt and how many LLM tokens enrichment would need (an estimate)."""
    heads = ctx.store.heads()
    cfg = ctx.project.sentinel
    plan: list[dict[str, Any]] = []
    for name in enabled_repos(ctx.project, repos):
        repo = ctx.project.repo(name)
        try:
            sha = ctx.reader.head(repo)
        except Exception as exc:
            plan.append({"repo": name, "action": "error", "error": str(exc)})
            continue
        head = heads.get(name)
        rebuild = force or head is None or head["sha"] != sha or head["ontology_version"] != ctx.ontology.version
        llm = repo.sentinel.llm or cfg.llm
        enrich_nodes = 0
        if llm != "off" and repo.sentinel.enrich and head:
            nodes = ctx.store.repo_nodes(name, head["sha"])
            enrich_nodes = sum(
                1
                for n in nodes.values()
                if n.tier in cfg.enrichment.tiers
                and n.type in ("Module", "Service", "ApiEndpoint", "DataModel", "Class")
                and not n.summary
            )
        plan.append(
            {
                "repo": name,
                "action": "rebuild" if rebuild else "skip (unchanged)",
                "head": head["sha"][:12] if head else None,
                "current": sha[:12],
                "llm": llm,
                "estimated_llm_tokens": enrich_nodes * tokens_per_node if llm != "off" else 0,
                "estimate_note": "nodes without a summary x average prompt size; unchanged blobs hit the cache",
            }
        )
    return plan
