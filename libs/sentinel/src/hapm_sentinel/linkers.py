"""Deterministic linkers: architecture edges inside a repository, and links between repositories.

Inside a repository: Repository and Service nodes, API endpoints, topics, tables, data models, tests and
Jira keys. Across repositories: HTTP calls to endpoints, shared libraries from package manifests, Python
imports and calls into another repository's modules, and SCIP global-symbol references. Every edge carries
evidence. A match that is not certain is AMBIGUOUS, never EXTRACTED.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from hapm_config import RepoConfig, SentinelConfig

from hapm_sentinel.engines.ast_py import dotted_names
from hapm_sentinel.facts import RepoFacts
from hapm_sentinel.ids import (
    endpoint_id,
    module_id,
    normalise_route,
    repo_id,
    route_pattern,
    service_id,
    symbol_id,
    table_id,
    ticket_id,
    topic_id,
)
from hapm_sentinel.model import Edge, Node

JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
PROV = ["linker:1"]


@dataclass
class RepoView:
    name: str
    sha: str
    config: RepoConfig
    nodes: dict[str, Node]
    facts: RepoFacts
    commits: list[dict[str, Any]] = field(default_factory=list)
    exports: dict[str, str] = field(default_factory=dict)
    external_refs: list[dict[str, Any]] = field(default_factory=list)
    package: str = ""


@dataclass
class LinkResult:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    report: dict[str, dict[str, Any]] = field(default_factory=dict)


def _enclosing(view: RepoView, path: str, line: int, symbol: str | None) -> str:
    """The function or class a fact belongs to; the module when it is at module level."""
    if symbol:
        sid = symbol_id(view.name, path, symbol)
        if sid in view.nodes:
            return sid
    best: tuple[int, str] | None = None
    for node in view.nodes.values():
        if node.source_file != path or node.type not in ("Function", "Test", "Class") or node.line_start is None:
            continue
        end = node.line_end if node.line_end is not None else node.line_start
        if node.line_start <= line <= max(end, node.line_start) and (best is None or node.line_start > best[0]):
            best = (node.line_start, node.id)
    if best is None:  # graphify gives start lines only: nearest preceding function in the file
        candidates = [
            (n.line_start, n.id)
            for n in view.nodes.values()
            if n.source_file == path
            and n.type in ("Function", "Test")
            and n.line_start is not None
            and n.line_start <= line
        ]
        best = max(candidates) if candidates else None
    return best[1] if best else module_id(view.name, path)


def intra_repo(view: RepoView) -> tuple[dict[str, Node], list[Edge]]:
    repo, sha = view.name, view.sha
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def add(node: Node) -> str:
        nodes.setdefault(node.id, node)
        return node.id

    def edge(
        source: str, target: str, relation: str, confidence: str, file: str | None, line: int | None, **attrs: Any
    ) -> None:
        edges.append(
            Edge(source, target, relation, confidence, repo, sha, file, line, line, list(PROV), attrs=dict(attrs))
        )

    rid = add(Node(repo_id(repo), "Repository", repo, repo, sha, provenance=list(PROV)))
    sid = add(Node(service_id(repo), "Service", repo, repo, sha, provenance=list(PROV)))
    edge(rid, sid, "CONTAINS", "EXTRACTED", None, None)
    for node in view.nodes.values():
        if node.type in ("Module", "File", "Config"):
            edge(rid, node.id, "CONTAINS", "EXTRACTED", node.source_file, 1)

    for ep in view.facts.endpoints:
        eid = add(
            Node(
                endpoint_id(repo, ep["method"], ep["path"]),
                "ApiEndpoint",
                f"{ep['method']} {normalise_route(ep['path'])}",
                repo,
                sha,
                ep["file"],
                ep["line"],
                ep.get("line_end"),
                provenance=list(PROV),
                attrs={"method": ep["method"], "path": normalise_route(ep["path"]), "framework": ep.get("framework")},
            )
        )
        edge(sid, eid, "EXPOSES", "EXTRACTED", ep["file"], ep["line"])
        handler = _enclosing(view, ep["file"], ep["line"], ep.get("symbol"))
        if "#" in handler:
            edge(handler, eid, "EXPOSES", "EXTRACTED", ep["file"], ep["line"])

    for dm in view.facts.data_models:
        did = symbol_id(repo, dm["file"], dm["symbol"])
        if did in view.nodes:
            view.nodes[did].type = "DataModel"
            tid = add(Node(table_id(dm["table"]), "Table", dm["table"].lower(), repo, sha, provenance=list(PROV)))
            edge(did, tid, "REFERENCES", "EXTRACTED", dm["file"], dm["line"])

    for t in view.facts.topics:
        tid = add(Node(topic_id(t["topic"]), "Topic", t["topic"], repo, sha, provenance=list(PROV)))
        source = _enclosing(view, t["file"], t["line"], t.get("symbol"))
        edge(source, tid, t["direction"], "EXTRACTED", t["file"], t["line"])
        edge(sid, tid, t["direction"], "EXTRACTED", t["file"], t["line"])

    for t in view.facts.tables:
        tid = add(Node(table_id(t["table"]), "Table", t["table"].lower(), repo, sha, provenance=list(PROV)))
        source = _enclosing(view, t["file"], t["line"], t.get("symbol"))
        edge(source, tid, t["access"], "EXTRACTED", t["file"], t["line"])
        edge(sid, tid, t["access"], "EXTRACTED", t["file"], t["line"])

    for commit in view.commits:
        message = str(commit.get("message", ""))
        for key in sorted(set(JIRA_KEY.findall(message))):
            kid = add(Node(ticket_id(key), "Ticket", key, repo, sha, provenance=list(PROV)))
            edges.append(
                Edge(
                    rid,
                    kid,
                    "REFERENCES_TICKET",
                    "EXTRACTED",
                    repo,
                    sha,
                    None,
                    None,
                    None,
                    list(PROV),
                    attrs={"commit": commit.get("sha"), "message": message[:200]},
                )
            )
    return nodes, edges


def derive_tests(view: RepoView, edges: list[Edge]) -> list[Edge]:
    """Code that a test calls or imports is TESTED_BY that test (INFERRED: a call is not proof of coverage)."""
    out: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for e in edges:
        if e.relation not in ("CALLS", "IMPORTS", "REFERENCES"):
            continue
        src, dst = view.nodes.get(e.source), view.nodes.get(e.target)
        if src is None or dst is None:
            continue
        src_is_test = src.type == "Test" or (src.type == "Module" and _test_module(src.source_file))
        dst_is_code = dst.type in ("Module", "Class", "DataModel", "Function") and not _test_module(dst.source_file)
        if src_is_test and dst_is_code and (dst.id, src.id) not in seen:
            seen.add((dst.id, src.id))
            out.append(
                Edge(
                    dst.id,
                    src.id,
                    "TESTED_BY",
                    "INFERRED",
                    view.name,
                    view.sha,
                    e.file,
                    e.line_start,
                    e.line_end,
                    list(PROV),
                )
            )
    return out


def _test_module(path: str | None) -> bool:
    from hapm_sentinel.engines.ast_py import is_test_path

    return bool(path) and is_test_path(path or "")


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3]) or (0,)


def satisfies(version: str, spec: str) -> bool | None:
    """True or False when the spec is understood; None when it is not (or no version is known)."""
    if not version:
        return None
    v = _version_tuple(version)
    clauses = re.findall(r"(>=|<=|==|~=|!=|>|<|\^|~)\s*v?([0-9][\w\.]*)", spec)
    if not clauses:
        bare = re.fullmatch(r"\s*v?([0-9][\w\.]*)\s*", spec.split("@")[-1])
        return (_version_tuple(bare.group(1)) == v) if bare else None
    for op, raw in clauses:
        w = _version_tuple(raw)
        ok = {
            ">=": v >= w,
            "<=": v <= w,
            ">": v > w,
            "<": v < w,
            "==": v == w,
            "!=": v != w,
            "~=": v >= w and v[: max(len(w) - 1, 1)] == w[: max(len(w) - 1, 1)],
            "^": v >= w and v[:1] == w[:1],
            "~": v >= w and v[:2] == w[:2],
        }[op]
        if not ok:
            return False
    return True


def _resolve_host(host: str, views: dict[str, RepoView], aliases: dict[str, str]) -> tuple[str | None, str]:
    if host in aliases and aliases[host] in views:
        return aliases[host], "EXTRACTED"
    for view in views.values():
        if host in view.config.sentinel.service_names:
            return view.name, "EXTRACTED"
    norm = re.sub(r"[^a-z0-9]", "", host.lower())
    for view in views.values():
        if re.sub(r"[^a-z0-9]", "", view.name.lower()) == norm:
            return view.name, "INFERRED"
    return None, "AMBIGUOUS"


def _call_target(view: RepoView, url: str) -> tuple[str | None, str]:
    """(host, path) of an HTTP call; a leading {CONSTANT} is looked up in the repository's string constants."""
    m = re.match(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}(.*)$", url)
    if m:
        base = next((consts[m.group(1)] for consts in view.facts.constants.values() if m.group(1) in consts), None)
        if base is None:
            return None, m.group(2)
        url = base.rstrip("/") + m.group(2)
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return parsed.hostname, parsed.path or "/"
    return None, url


def cross_repo(views: dict[str, RepoView], config: SentinelConfig) -> LinkResult:
    result = LinkResult()
    report: dict[str, dict[str, Any]] = defaultdict(lambda: {"outbound": 0, "inbound": 0, "unresolved": []})

    def link(
        src_view: RepoView,
        source: str,
        target: str,
        target_repo: str,
        relation: str,
        confidence: str,
        file: str | None,
        line: int | None,
        **attrs: Any,
    ) -> None:
        result.edges.append(
            Edge(
                source,
                target,
                relation,
                confidence,
                src_view.name,
                src_view.sha,
                file,
                line,
                line,
                list(PROV),
                cross_repo=True,
                attrs={"target_repo": target_repo, **attrs},
                layer="link",
            )
        )
        report[src_view.name]["outbound"] += 1
        report[target_repo]["inbound"] += 1

    # Manifests: which repository publishes which package, and who depends on it.
    publishes: dict[str, tuple[str, dict[str, Any]]] = {}
    for view in views.values():
        for m in view.facts.manifests:
            publishes[m["name"]] = (view.name, m)
    depends: dict[tuple[str, str], bool | None] = {}
    for view in views.values():
        for m in view.facts.manifests:
            for dep in m["deps"]:
                if dep in publishes and publishes[dep][0] != view.name:
                    target_repo, target_manifest = publishes[dep]
                    ok = satisfies(target_manifest.get("version", ""), m["dep_specs"].get(dep, ""))
                    depends[(view.name, target_repo)] = ok
                    confidence = "AMBIGUOUS" if ok is False else "EXTRACTED"
                    line = _line_of(m, dep)
                    link(
                        view,
                        repo_id(view.name),
                        repo_id(target_repo),
                        target_repo,
                        "DEPENDS_ON",
                        confidence,
                        m["file"],
                        line,
                        spec=m["dep_specs"].get(dep, ""),
                        version=target_manifest.get("version"),
                    )

    def dep_confidence(src: str, dst: str) -> str:
        ok = depends.get((src, dst), "none")
        return "AMBIGUOUS" if ok is False else ("EXTRACTED" if ok is True else "INFERRED")

    # HTTP calls to endpoints in other repositories.
    endpoints: dict[str, list[Node]] = defaultdict(list)
    for view in views.values():
        for node in view.nodes.values():
            if node.type == "ApiEndpoint":
                endpoints[view.name].append(node)
    for view in views.values():
        for call in view.facts.http_calls:
            host, path = _call_target(view, call["url"])
            path = normalise_route(path.split("?", 1)[0])
            sample = re.sub(r"\{[^}]+\}", "x", path)
            source = _enclosing(view, call["file"], call["line"], call.get("symbol"))
            candidates: list[tuple[str, Node]] = []
            confidence = "AMBIGUOUS"
            if host:
                resolved, confidence = _resolve_host(host, views, config.service_aliases)
                repos = [resolved] if resolved else []
            else:
                repos = [r for r in views if r != view.name]
            for r in repos:
                for ep in endpoints.get(r, []):
                    if ep.attrs.get("method") == call["method"] and route_pattern(str(ep.attrs.get("path", ""))).match(
                        sample
                    ):
                        candidates.append((r, ep))
            if not candidates:
                report[view.name]["unresolved"].append(
                    {"kind": "http", "file": call["file"], "line": call["line"], "url": call["url"], "host": host}
                )
                continue
            if len(candidates) > 1 or not host:
                confidence = "AMBIGUOUS"
            for target_repo, ep in candidates:
                link(
                    view,
                    source,
                    ep.id,
                    target_repo,
                    "CONSUMES",
                    confidence,
                    call["file"],
                    call["line"],
                    url=call["url"],
                )
                link(
                    view,
                    service_id(view.name),
                    ep.id,
                    target_repo,
                    "CONSUMES",
                    confidence,
                    call["file"],
                    call["line"],
                    url=call["url"],
                )

    # Python modules each repository defines, by import name.
    modules_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for view in views.values():
        for node in view.nodes.values():
            if node.type == "Module" and (node.source_file or "").endswith(".py"):
                for name in dotted_names(node.source_file or ""):
                    modules_by_name[name].append((view.name, node.source_file or ""))
        for ns, mid in view.exports.items():
            path = mid.split(":", 1)[1]
            if (view.name, path) not in modules_by_name[ns]:
                modules_by_name[ns].append((view.name, path))

    def other_repo_module(view: RepoView, module: str) -> list[tuple[str, str]]:
        own = [m for m in modules_by_name.get(module, []) if m[0] == view.name]
        return [] if own else [m for m in modules_by_name.get(module, []) if m[0] != view.name]

    seen: set[tuple[str, str, str]] = set()
    for view in views.values():
        for imp in view.facts.imports:
            for target_repo, path in other_repo_module(view, imp["module"]):
                key = (module_id(view.name, imp["file"]), module_id(target_repo, path), "IMPORTS")
                if key in seen:
                    continue
                seen.add(key)
                confidence = dep_confidence(view.name, target_repo)
                if len(other_repo_module(view, imp["module"])) > 1:
                    confidence = "AMBIGUOUS"
                link(
                    view,
                    key[0],
                    key[1],
                    target_repo,
                    "IMPORTS",
                    confidence,
                    imp["file"],
                    imp["line"],
                    module=imp["module"],
                )
        for call in view.facts.import_calls:
            for target_repo, path in other_repo_module(view, call["module"]):
                target = symbol_id(target_repo, path, call["name"])
                if target not in views[target_repo].nodes:
                    continue
                source = _enclosing(view, call["file"], call["line"], call.get("symbol"))
                key = (source, target, "CALLS")
                if key in seen or "#" not in source:
                    continue
                seen.add(key)
                link(
                    view,
                    source,
                    target,
                    target_repo,
                    "CALLS",
                    dep_confidence(view.name, target_repo),
                    call["file"],
                    call["line"],
                )
        # SCIP global symbols the repository's own index could not resolve.
        for ref in view.external_refs:
            for target_repo, path in other_repo_module(view, ref["module"]):
                target = (
                    symbol_id(target_repo, path, ref["qualname"])
                    if ref.get("qualname")
                    else module_id(target_repo, path)
                )
                if target not in views[target_repo].nodes:
                    target = module_id(target_repo, path)
                relation = (
                    "CALLS" if ref["context"] == "call" and ref.get("kind") == "method" and "#" in target else "IMPORTS"
                )
                source = ref["from"] if relation == "CALLS" else module_id(view.name, ref["file"])
                key = (source, target, relation)
                if key in seen:
                    continue
                seen.add(key)
                link(
                    view,
                    source,
                    target,
                    target_repo,
                    relation,
                    dep_confidence(view.name, target_repo),
                    ref["file"],
                    ref["line"],
                    via="scip",
                )

    for name in views:
        report[name]  # every repository appears in the report
    result.report = dict(report)
    return result


def _line_of(manifest: dict[str, Any], dep: str) -> int | None:
    line = manifest.get("dep_lines", {}).get(dep)
    return int(line) if line else None
