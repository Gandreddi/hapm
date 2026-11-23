"""Built-in Python extractor (fallback engine). No dependency beyond the standard library.

Two passes over every .py file: first collect definitions, then resolve imports, inheritance and calls
against those definitions. Direct name bindings are EXTRACTED; attribute calls resolved through `self`
or a module-level instance are INFERRED; anything unresolvable is dropped rather than guessed.
"""

import ast
import sys
from dataclasses import dataclass, field

from hapm_codeintel.snapshot import Snapshot

from hapm_sentinel.ids import lib_id, module_id, symbol_id
from hapm_sentinel.model import Edge, Fragment, Node

VERSION = "1"
STDLIB = set(sys.stdlib_module_names)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_test.py")


def dotted_names(path: str) -> list[str]:
    """Import names a file can be reached by: 'src/pay/api.py' -> ['src.pay.api', 'pay.api']."""
    stem = path[:-3]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    dotted = stem.replace("/", ".")
    names = [dotted]
    if dotted.startswith("src."):
        names.append(dotted[4:])
    return names


@dataclass
class Definition:
    path: str
    qual: str
    kind: str  # class | function
    line_start: int
    line_end: int
    bases: list[ast.expr] = field(default_factory=list)
    node: ast.AST | None = None
    is_test: bool = False
    is_model: bool = False


@dataclass
class ModuleInfo:
    path: str
    tree: ast.Module
    bindings: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # name -> (module dotted, symbol)
    instances: dict[str, ast.Name] = field(default_factory=dict)  # module-level name -> constructor name
    imports: list[tuple[str, int]] = field(default_factory=list)  # (dotted module, line)


def _resolve_relative(path: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    parts = path[:-3].split("/")[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    return f"{base}.{module}" if module else base


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, defs: dict[tuple[str, str], Definition]) -> None:
        self.path = path
        self.defs = defs
        self.scope: list[str] = []
        self.test_file = is_test_path(path)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        if any(k == "function" for k in self._kinds()):
            return
        qual = ".".join([*self.scope, node.name])
        model = any(
            isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)
            for s in node.body
        )
        self.defs[(self.path, qual)] = Definition(
            self.path,
            qual,
            "class",
            node.lineno,
            node.end_lineno or node.lineno,
            list(node.bases),
            node,
            is_test=self.test_file and (node.name.startswith("Test") or node.name.endswith("Test")),
            is_model=model,
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _kinds(self) -> list[str]:
        return [self.defs[(self.path, ".".join(self.scope[: i + 1]))].kind for i in range(len(self.scope))]

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if any(k == "function" for k in self._kinds()):
            return  # nested functions are part of their parent
        qual = ".".join([*self.scope, node.name])
        self.defs[(self.path, qual)] = Definition(
            self.path,
            qual,
            "function",
            node.lineno,
            node.end_lineno or node.lineno,
            node=node,
            is_test=self.test_file and node.name.startswith("test"),
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function(node)


def extract(snapshot: Snapshot) -> Fragment:
    frag = Fragment("ast", VERSION, snapshot.repo, snapshot.sha)
    repo, sha = snapshot.repo, snapshot.sha
    prov = [frag.provenance]
    modules: dict[str, ModuleInfo] = {}
    by_dotted: dict[str, str] = {}
    defs: dict[tuple[str, str], Definition] = {}
    for path, text in snapshot.files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            frag.rejected["file: python syntax error"] += 1
            continue
        modules[path] = ModuleInfo(path, tree)
        for name in dotted_names(path):
            by_dotted[name] = path
        _Collector(path, defs).visit(tree)
    if modules:
        frag.languages.add("python")

    def node_for(d: Definition) -> Node:
        kind = "Test" if d.is_test else ("DataModel" if d.is_model else ("Class" if d.kind == "class" else "Function"))
        return Node(
            symbol_id(repo, d.path, d.qual),
            kind,
            d.qual,
            repo,
            sha,
            d.path,
            d.line_start,
            d.line_end,
            provenance=list(prov),
        )

    for path in modules:
        lines = len(snapshot.files[path].splitlines()) or 1
        frag.add_node(Node(module_id(repo, path), "Module", path, repo, sha, path, 1, lines, provenance=list(prov)))
    for d in defs.values():
        frag.add_node(node_for(d))
        parent_qual = d.qual.rsplit(".", 1)[0] if "." in d.qual else None
        parent = symbol_id(repo, d.path, parent_qual) if parent_qual else module_id(repo, d.path)
        frag.add_edge(
            Edge(
                parent,
                symbol_id(repo, d.path, d.qual),
                "CONTAINS",
                "EXTRACTED",
                repo,
                sha,
                d.path,
                d.line_start,
                d.line_start,
                list(prov),
            )
        )

    def internal_symbol(module: str, name: str | None) -> str | None:
        path = by_dotted.get(module)
        if path is None:
            return None
        if name is None:
            return module_id(repo, path)
        if (path, name) in defs:
            return symbol_id(repo, path, name)
        sub = by_dotted.get(f"{module}.{name}")
        return module_id(repo, sub) if sub else None

    # Imports and module-level instance bindings.
    for path, info in modules.items():
        mod = module_id(repo, path)
        for stmt in ast.walk(info.tree):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    info.bindings[(alias.asname or alias.name).split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0],
                        None,
                    )
                    info.imports.append((alias.name, stmt.lineno))
            elif isinstance(stmt, ast.ImportFrom):
                module = _resolve_relative(path, stmt.module, stmt.level)
                info.imports.append((module, stmt.lineno))
                for alias in stmt.names:
                    info.bindings[alias.asname or alias.name] = (module, alias.name)
        for stmt in info.tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
            ):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        info.instances[t.id] = stmt.value.func
        seen: set[str] = set()
        for module, line in info.imports:
            target: str | None = internal_symbol(module, None)
            if target is None:
                top = module.split(".")[0]
                if not top or top in STDLIB:
                    continue
                target = lib_id(top)
                frag.add_node(Node(target, "ExternalLibrary", top, repo, sha, provenance=list(prov)))
            if target in seen or target == mod:
                continue
            seen.add(target)
            frag.add_edge(Edge(mod, target, "IMPORTS", "EXTRACTED", repo, sha, path, line, line, list(prov)))

    def resolve_name(info: ModuleInfo, name: str) -> str | None:
        if (info.path, name) in defs:
            return symbol_id(repo, info.path, name)
        if name in info.bindings:
            module, symbol = info.bindings[name]
            return internal_symbol(module, symbol)
        return None

    def method_of(path: str, cls: str, method: str, depth: int = 0) -> str | None:
        if (path, f"{cls}.{method}") in defs:
            return symbol_id(repo, path, f"{cls}.{method}")
        d = defs.get((path, cls))
        if d is None or depth > 5:
            return None
        for base in d.bases:
            if isinstance(base, ast.Name):
                resolved = resolve_name(modules[path], base.id)
                if resolved and "#" in resolved:
                    bpath, bqual = resolved.split(":", 1)[1].split("#", 1)
                    found = method_of(bpath, bqual, method, depth + 1)
                    if found:
                        return found
        return None

    # Inheritance and calls.
    for d in defs.values():
        info = modules[d.path]
        src = symbol_id(repo, d.path, d.qual)
        if d.kind == "class":
            for base in d.bases:
                base_name = (
                    base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
                )
                if base_name is None:
                    continue
                target = resolve_name(info, base_name)
                if target is None and base_name in info.bindings:
                    top = info.bindings[base_name][0].split(".")[0]
                    if top and top not in STDLIB:
                        target = lib_id(top)
                        frag.add_node(Node(target, "ExternalLibrary", top, repo, sha, provenance=list(prov)))
                if target and target != src:
                    frag.add_edge(
                        Edge(
                            src,
                            target,
                            "INHERITS",
                            "EXTRACTED",
                            repo,
                            sha,
                            d.path,
                            d.line_start,
                            d.line_start,
                            list(prov),
                        )
                    )
            continue
        cls = d.qual.rsplit(".", 1)[0] if "." in d.qual else None
        seen_calls: set[str] = set()
        assert d.node is not None
        for call in ast.walk(d.node):
            if not isinstance(call, ast.Call):
                continue
            target = None
            confidence = "EXTRACTED"
            func = call.func
            if isinstance(func, ast.Name):
                target = resolve_name(info, func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                owner = func.value.id
                if owner == "self" and cls:
                    target, confidence = method_of(d.path, cls, func.attr), "INFERRED"
                elif owner in info.instances:
                    cls_id = resolve_name(info, info.instances[owner].id)
                    if cls_id and "#" in cls_id:
                        cpath, cqual = cls_id.split(":", 1)[1].split("#", 1)
                        if (cpath, cqual) in defs and defs[(cpath, cqual)].kind == "class":
                            target, confidence = method_of(cpath, cqual, func.attr), "INFERRED"
                elif owner in info.bindings and info.bindings[owner][1] is None:
                    target = internal_symbol(info.bindings[owner][0], func.attr)
            if target is None or "#" not in target or target == src or target in seen_calls:
                continue
            seen_calls.add(target)
            frag.add_edge(
                Edge(src, target, "CALLS", confidence, repo, sha, d.path, call.lineno, call.lineno, list(prov))
            )
    return frag
