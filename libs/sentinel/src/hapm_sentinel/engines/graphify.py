"""Map graphify's graph.json (pinned version, ADR-0021) into the Sentinel ontology.

graphify runs in code-only mode with every LLM backend disabled; Sentinel enriches later through the
gateway. Unmapped relations are rejected and counted, never guessed. graphify's EXTRACTED, INFERRED and
AMBIGUOUS tags are kept unchanged.
"""

import ast
import re
from typing import Any

from hapm_codeintel.snapshot import Snapshot

from hapm_sentinel.engines.ast_py import STDLIB, is_test_path
from hapm_sentinel.ids import lib_id, module_id, symbol_id
from hapm_sentinel.model import Edge, Fragment, Node

SCHEMA_VERSION = 1
CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".cs", ".rb", ".kt", ".rs", ".php", ".scala")
MANIFESTS = ("pyproject.toml", "package.json", "go.mod", "pom.xml", "requirements.txt", "setup.cfg")
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".cs": "csharp",
}
REQUIRED_NODE_KEYS = {"id", "label", "source_file", "source_location"}
REQUIRED_EDGE_KEYS = {"source", "target", "relation", "confidence"}
TS_IMPORT = re.compile(r"""from\s+["']([^"']+)["']|require\(\s*["']([^"']+)["']\s*\)""")


class GraphifySchemaError(ValueError):
    """graphify's output format changed; refuse to map it silently."""


def check_schema(graph: dict[str, Any], pinned_version: str) -> None:
    meta = graph.get("graph", {})
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise GraphifySchemaError(f"graph.schema_version is {meta.get('schema_version')}, expected {SCHEMA_VERSION}")
    if pinned_version and meta.get("graphify_version") != pinned_version:
        raise GraphifySchemaError(f"graphify_version is {meta.get('graphify_version')}, pinned {pinned_version}")
    if "nodes" not in graph or ("links" not in graph and "edges" not in graph):
        raise GraphifySchemaError("graph.json has no nodes or links")
    for n in graph["nodes"]:
        # Every node has an id and label; code nodes also carry their file and location (external ones do not).
        required = REQUIRED_NODE_KEYS if n.get("file_type") == "code" else {"id", "label"}
        if not required <= n.keys():
            raise GraphifySchemaError(f"node {n.get('id')} lacks {sorted(required - n.keys())}")
    for e in graph.get("links", graph.get("edges", [])):
        if not REQUIRED_EDGE_KEYS <= e.keys():
            raise GraphifySchemaError(f"edge lacks {sorted(REQUIRED_EDGE_KEYS - e.keys())}")


def _line(location: str | None) -> int | None:
    if not location:
        return None
    m = re.match(r"L(\d+)", location)
    return int(m.group(1)) if m else None


def _import_module(snapshot: Snapshot, path: str | None, line: int | None) -> str | None:
    """Read the actual import statement at the evidence line to name an external module."""
    if not path or not line or path not in snapshot.files:
        return None
    lines = snapshot.files[path].splitlines()
    if line > len(lines):
        return None
    text = lines[line - 1].strip()
    if path.endswith(".py"):
        try:
            stmt = ast.parse(text).body[0]
        except (SyntaxError, IndexError):
            return None
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            return stmt.module.split(".")[0]
        if isinstance(stmt, ast.Import):
            return stmt.names[0].name.split(".")[0]
        return None
    m = TS_IMPORT.search(text)
    spec = (m.group(1) or m.group(2)) if m else None
    if spec and not spec.startswith("."):
        return "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
    return None


def map_graph(graph: dict[str, Any], snapshot: Snapshot, version: str, relation_map: dict[str, str]) -> Fragment:
    check_schema(graph, version)
    repo, sha = snapshot.repo, snapshot.sha
    frag = Fragment("graphify", version, repo, sha)
    prov = [frag.provenance]
    raw_nodes = {n["id"]: n for n in graph["nodes"]}
    raw_edges = graph.get("links", graph.get("edges", []))
    parent_of: dict[str, str] = {e["target"]: e["source"] for e in raw_edges if e["relation"] == "method"}
    mapped: dict[str, str] = {}  # graphify id -> sentinel id

    def is_file_node(n: dict[str, Any]) -> bool:
        path = n.get("source_file") or ""
        return bool(path) and n["label"] == path.rsplit("/", 1)[-1]

    def qualname(n: dict[str, Any], depth: int = 0) -> str:
        if n.get("_callable_class"):
            return str((n.get("metadata") or {}).get("type_path") or n["label"])
        name = str(n["label"]).lstrip(".").removesuffix("()")
        parent = parent_of.get(n["id"])
        if parent and parent in raw_nodes and depth < 5:
            return f"{qualname(raw_nodes[parent], depth + 1)}.{name}"
        return name

    for n in graph["nodes"]:
        path = n.get("source_file") or ""
        file_type = n.get("file_type")
        if is_file_node(n):
            name = path.rsplit("/", 1)[-1]
            if name in MANIFESTS:
                kind = "Config"
            elif path.endswith(CODE_SUFFIXES):
                kind = "Module"
                lang = LANGUAGE_BY_SUFFIX.get("." + path.rsplit(".", 1)[-1])
                if lang:
                    frag.languages.add(lang)
            else:
                kind = "File"
            lines = len(snapshot.files.get(path, "").splitlines()) or 1
            node = Node(module_id(repo, path), kind, path, repo, sha, path, 1, lines, provenance=list(prov))
        elif n.get("_callable") and path and file_type == "code":
            qual = qualname(n)
            test = is_test_path(path)
            if n.get("_callable_class"):
                kind = "Test" if test and (qual.startswith("Test") or qual.endswith("Test")) else "Class"
            else:
                kind = "Test" if test and qual.rsplit(".", 1)[-1].startswith("test") else "Function"
            line = _line(n.get("source_location"))
            node = Node(symbol_id(repo, path, qual), kind, qual, repo, sha, path, line, None, provenance=list(prov))
            if n.get("community") is not None:
                node.attrs["community"] = n.get("community")
        else:
            continue
        mapped[n["id"]] = frag.add_node(node).id

    # External libraries come from import edges; the import statement itself names the module.
    for e in raw_edges:
        if e["relation"] in ("imports", "imports_from") and e["target"] not in mapped and e["source"] in mapped:
            src_path = raw_nodes[e["source"]].get("source_file") or e.get("source_file")
            top = _import_module(snapshot, e.get("source_file") or src_path, _line(e.get("source_location")))
            if top and top not in STDLIB:
                lib = frag.add_node(Node(lib_id(top), "ExternalLibrary", top, repo, sha, provenance=list(prov)))
                mapped[e["target"]] = lib.id

    for n in graph["nodes"]:
        if n["id"] in mapped:
            continue
        frag.rejected[f"node: graphify {n.get('file_type') or 'external'} not in ontology"] += 1

    seen: set[tuple[str, str, str]] = set()
    for e in raw_edges:
        relation = e["relation"]
        if relation == "rationale_for":
            target = mapped.get(e["target"])
            if target and target in frag.nodes:
                doc = str(raw_nodes.get(e["source"], {}).get("label", ""))[:300]
                frag.nodes[target].attrs["doc"] = doc  # untrusted repository text, stored as data only
            frag.rejected["edge: rationale_for (kept as doc attribute)"] += 1
            continue
        onto = relation_map.get(relation)
        if onto is None:
            frag.rejected[f"edge: unmapped graphify relation {relation}"] += 1
            continue
        source, target = mapped.get(e["source"]), mapped.get(e["target"])
        if source is None or target is None:
            frag.rejected[f"edge: {relation} to a node outside the ontology"] += 1
            continue
        if onto == "IMPORTS":
            # Sentinel imports are module to module (or library): collapse symbol targets to their module.
            if "#" in target:
                target = target.split("#", 1)[0]
            if "#" in source:
                source = source.split("#", 1)[0]
        if source == target:
            continue
        key = (source, target, onto)
        if key in seen:
            continue
        seen.add(key)
        line = _line(e.get("source_location"))
        edge = Edge(
            source, target, onto, str(e["confidence"]), repo, sha, e.get("source_file") or None, line, line, list(prov)
        )
        if "confidence_score" in e:
            edge.attrs["confidence_score"] = e["confidence_score"]
        frag.add_edge(edge)
    return frag
