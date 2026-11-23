"""Map a SCIP index (`scip print --json`) into the Sentinel ontology with exact evidence.

Definitions create or confirm Class and Function nodes with their full line ranges. References become
IMPORTS (module level), CALLS (a callable referenced inside a function body) or REFERENCES (anything else).
Symbols the index does not define are kept as external references so the cross-repository linker can
match them against other repositories' SCIP indexes by their global symbol (package and descriptor).
"""

from dataclasses import dataclass
from typing import Any

from hapm_codeintel.snapshot import Snapshot

from hapm_sentinel.engines.ast_py import is_test_path
from hapm_sentinel.ids import module_id, symbol_id
from hapm_sentinel.model import Edge, Fragment, Node

DEFINITION = 1
IMPORT = 2
SUFFIX = {"/": "namespace", "#": "type", ".": "term", ":": "meta", "!": "macro"}
STDLIB_PACKAGES = {"python-stdlib"}


@dataclass(frozen=True)
class Symbol:
    raw: str
    manager: str
    package: str
    version: str
    namespace: tuple[str, ...]
    members: tuple[tuple[str, str], ...]  # (name, kind) for type, term and method descriptors

    @property
    def qualname(self) -> str:
        return ".".join(name for name, _ in self.members)

    @property
    def kind(self) -> str:
        return self.members[-1][1] if self.members else "module"


def _name(text: str, i: int) -> tuple[str, int]:
    if text[i] == "`":
        out, i = [], i + 1
        while i < len(text):
            if text[i] == "`":
                if i + 1 < len(text) and text[i + 1] == "`":
                    out.append("`")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(text[i])
            i += 1
        raise ValueError("unterminated backtick name")
    start = i
    while i < len(text) and (text[i].isalnum() or text[i] in "_+-$"):
        i += 1
    return text[start:i], i


def parse_symbol(raw: str) -> Symbol | None:
    """Parse a global SCIP symbol; None for local symbols or anything malformed."""
    if raw.startswith("local "):
        return None
    parts = raw.split(" ", 4)
    if len(parts) < 5:
        return None
    _, manager, package, version, text = parts
    namespace: list[str] = []
    members: list[tuple[str, str]] = []
    i = 0
    try:
        while i < len(text):
            if text[i] == "[":  # type parameter
                _, i = _name(text, i + 1)
                i += 1
                continue
            if text[i] == "(":  # parameter
                _, i = _name(text, i + 1)
                i += 1
                continue
            name, i = _name(text, i)
            if i >= len(text):
                break
            ch = text[i]
            if ch == "(":  # method: name(disambiguator).
                close = text.index(")", i)
                i = close + 2
                members.append((name, "method"))
                continue
            kind = SUFFIX.get(ch)
            i += 1
            if kind == "namespace":
                namespace.append(name)
            elif kind in ("type", "term"):
                members.append((name, kind))
    except ValueError:
        return None
    return Symbol(raw, manager, package, version, tuple(namespace), tuple(members))


def _is_call(text: str, rng: list[int]) -> bool:
    """SCIP records references, not calls: a reference is a call when the next character is '('."""
    lines = text.splitlines()
    line = rng[0]
    end_line, end_char = (rng[2], rng[3]) if len(rng) == 4 else (line, rng[2])
    if end_line >= len(lines):
        return False
    rest = lines[end_line][end_char:].lstrip()
    return rest.startswith("(")


def _lines(occ: dict[str, Any]) -> tuple[int, int]:
    rng = occ.get("enclosing_range") or occ["range"]
    start = rng[0] + 1
    end = (rng[2] + 1) if len(rng) == 4 else start
    return start, end


def map_index(index: dict[str, Any], snapshot: Snapshot, indexer: str) -> Fragment:
    repo, sha = snapshot.repo, snapshot.sha
    tool = index.get("metadata", {}).get("tool_info", {})
    frag = Fragment("scip", f"{tool.get('name', indexer)}-{tool.get('version', '?')}", repo, sha)
    prov = [f"scip:{tool.get('name', indexer)}@{tool.get('version', '?')}"]
    docs = index.get("documents", [])

    # Which namespace (module) each document defines, and the project's own package name.
    ns_to_path: dict[tuple[str, ...], str] = {}
    packages: dict[str, int] = {}
    for doc in docs:
        path = doc["relative_path"]
        counts: dict[tuple[str, ...], int] = {}
        for occ in doc.get("occurrences", []):
            if occ.get("symbol_roles", 0) & DEFINITION:
                sym = parse_symbol(occ["symbol"])
                if sym and sym.namespace:
                    counts[sym.namespace] = counts.get(sym.namespace, 0) + 1
                    packages[sym.package] = packages.get(sym.package, 0) + 1
        if counts:
            ns_to_path[max(counts, key=lambda k: counts[k])] = path
    own_package = max(packages, key=lambda k: packages[k]) if packages else ""

    def internal(sym: Symbol) -> tuple[str, str] | None:
        path = ns_to_path.get(sym.namespace)
        return (path, sym.qualname) if path and sym.package == own_package else None

    # Definitions: modules, classes, functions with full ranges.
    defs: dict[str, list[tuple[int, int, str]]] = {}  # path -> [(start, end, node id)] for functions
    for doc in docs:
        path = doc["relative_path"]
        if path not in snapshot.files:
            frag.rejected["scip: document not in snapshot"] += 1
            continue
        lines = len(snapshot.files[path].splitlines()) or 1
        frag.add_node(Node(module_id(repo, path), "Module", path, repo, sha, path, 1, lines, provenance=list(prov)))
        lang = "python" if path.endswith(".py") else ("typescript" if path.endswith((".ts", ".tsx")) else None)
        if lang:
            frag.languages.add(lang)
        for occ in doc.get("occurrences", []):
            if not occ.get("symbol_roles", 0) & DEFINITION:
                continue
            sym = parse_symbol(occ["symbol"])
            if sym is None or not sym.members or internal(sym) is None:
                continue
            if any(kind == "term" for _, kind in sym.members) or sym.kind not in ("type", "method"):
                continue  # variables and fields are not ontology nodes
            if any(kind == "method" for _, kind in sym.members[:-1]):
                continue  # functions nested in functions belong to their parent, as in the ast engine
            start, end = _lines(occ)
            qual = sym.qualname
            test = is_test_path(path)
            leaf = sym.members[-1][0]
            if sym.kind == "type":
                kind = "Test" if test and (leaf.startswith("Test") or leaf.endswith("Test")) else "Class"
            else:
                kind = "Test" if test and leaf.startswith("test") else "Function"
            nid = symbol_id(repo, path, qual)
            frag.add_node(Node(nid, kind, qual, repo, sha, path, start, end, provenance=list(prov)))
            parent = symbol_id(repo, path, qual.rsplit(".", 1)[0]) if "." in qual else module_id(repo, path)
            frag.add_edge(Edge(parent, nid, "CONTAINS", "EXTRACTED", repo, sha, path, start, start, list(prov)))
            if sym.kind == "method":
                defs.setdefault(path, []).append((start, end, nid))

    # Inheritance from symbol relationships.
    for doc in docs:
        for info in doc.get("symbols", []):
            sym = parse_symbol(info.get("symbol", ""))
            loc = internal(sym) if sym else None
            if not sym or loc is None or sym.kind != "type":
                continue
            for rel in info.get("relationships", []):
                base = parse_symbol(rel.get("symbol", ""))
                base_loc = internal(base) if base else None
                if base_loc and rel.get("is_implementation"):
                    frag.add_edge(
                        Edge(
                            symbol_id(repo, *loc),
                            symbol_id(repo, *base_loc),
                            "INHERITS",
                            "EXTRACTED",
                            repo,
                            sha,
                            loc[0],
                            None,
                            None,
                            list(prov),
                        )
                    )

    seen: set[tuple[str, str, str]] = set()
    for doc in docs:
        path = doc["relative_path"]
        if path not in snapshot.files:
            continue
        mod = module_id(repo, path)
        funcs = sorted(defs.get(path, []), key=lambda d: d[1] - d[0])
        for occ in doc.get("occurrences", []):
            roles = occ.get("symbol_roles", 0)
            if roles & DEFINITION:
                continue
            sym = parse_symbol(occ["symbol"])
            if sym is None or sym.package in STDLIB_PACKAGES:
                continue
            line = occ["range"][0] + 1
            enclosing = next((nid for start, end, nid in funcs if start < line <= end), None)
            loc = internal(sym)
            if loc is None:
                if sym.namespace:
                    frag.external_refs.append(
                        {
                            "from": enclosing or mod,
                            "file": path,
                            "line": line,
                            "manager": sym.manager,
                            "package": sym.package,
                            "version": sym.version,
                            "module": ".".join(sym.namespace),
                            "qualname": sym.qualname,
                            "kind": sym.kind,
                            "context": "call" if enclosing else "import",
                        }
                    )
                continue
            tpath, qual = loc
            if enclosing is None:
                if tpath == path:
                    continue
                relation, source, target = "IMPORTS", mod, module_id(repo, tpath)
            elif sym.kind == "method" and _is_call(snapshot.files[path], occ["range"]):
                relation, source, target = "CALLS", enclosing, symbol_id(repo, tpath, qual)
            elif sym.kind == "method":
                relation, source, target = "REFERENCES", enclosing, symbol_id(repo, tpath, qual)
            elif sym.kind == "type":
                relation, source, target = "REFERENCES", enclosing, symbol_id(repo, tpath, qual)
            else:
                continue
            if source == target or (source, target, relation) in seen:
                continue
            seen.add((source, target, relation))
            frag.add_edge(Edge(source, target, relation, "EXTRACTED", repo, sha, path, line, line, list(prov)))
    frag.exports = {".".join(ns): module_id(repo, p) for ns, p in ns_to_path.items()}
    frag.package = own_package
    return frag
