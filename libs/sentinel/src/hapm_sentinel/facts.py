"""Deterministic facts the cross-repository linkers need, read by Sentinel's own parsers (no LLM, no engine).

Python is parsed with `ast`; TypeScript and JavaScript with conservative regular expressions. Every fact
carries the file and line it came from, so every linked edge has evidence.
"""

import ast
import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
CLIENT_NAMES = {"httpx", "requests", "client", "session", "http", "aiohttp"}
PUBLISH_NAMES = {"publish", "produce", "send_event", "emit"}
SUBSCRIBE_NAMES = {"subscribe", "consume", "listen", "on_topic"}
SQL_WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.I)
SQL_READ = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.I)
SQL_HINT = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.I)


@dataclass
class RepoFacts:
    constants: dict[str, dict[str, str]] = field(default_factory=dict)  # file -> NAME -> string value
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    http_calls: list[dict[str, Any]] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    data_models: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)  # python: file, line, module
    import_calls: list[dict[str, Any]] = field(default_factory=list)  # python: call of a from-imported name

    def as_dict(self) -> dict[str, Any]:
        return {
            "constants": self.constants,
            "endpoints": self.endpoints,
            "http_calls": self.http_calls,
            "topics": self.topics,
            "tables": self.tables,
            "manifests": self.manifests,
            "data_models": self.data_models,
            "imports": self.imports,
            "import_calls": self.import_calls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoFacts":
        return cls(**{k: data.get(k, {} if k == "constants" else []) for k in cls.__dataclass_fields__})


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def url_template(node: ast.AST | None) -> str | None:
    """'http://x/a' stays as is; f'{BASE}/inventory/{sku}' becomes '{BASE}/inventory/{sku}'."""
    literal = _const_str(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                inner = part.value
                name = (
                    inner.id
                    if isinstance(inner, ast.Name)
                    else (inner.attr if isinstance(inner, ast.Attribute) else "_")
                )
                out.append("{" + name + "}")
        return "".join(out)
    return None


def _call_name(func: ast.AST) -> tuple[str | None, str | None]:
    """(object name, attribute or function name) for x.y(...) and y(...)."""
    if isinstance(func, ast.Attribute):
        base = func.value
        obj = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
        return obj, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


class _PyFacts(ast.NodeVisitor):
    def __init__(self, path: str, facts: RepoFacts) -> None:
        self.path = path
        self.facts = facts
        self.scope: list[str] = []
        self.kinds: list[str] = []
        self.decorators: set[int] = set()
        self.bound: dict[str, tuple[str, str]] = {}  # name -> (module, symbol) from `from m import s`

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.facts.imports.append({"file": self.path, "line": node.lineno, "module": alias.name})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level == 0 and node.module:
            self.facts.imports.append({"file": self.path, "line": node.lineno, "module": node.module})
            for alias in node.names:
                self.bound[alias.asname or alias.name] = (node.module, alias.name)

    def _symbol(self) -> str | None:
        return ".".join(self.scope) or None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets
            ):
                table = _const_str(stmt.value)
                if table:
                    qual = ".".join([*self.scope, node.name])
                    self.facts.data_models.append(
                        {"file": self.path, "line": node.lineno, "symbol": qual, "table": table}
                    )
        self.scope.append(node.name)
        self.kinds.append("class")
        self.generic_visit(node)
        self.scope.pop()
        self.kinds.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = ".".join([*self.scope, node.name])
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            self.decorators.add(id(deco))
            obj, name = _call_name(deco.func)
            first = _const_str(deco.args[0]) if deco.args else None
            if name in HTTP_METHODS and first is not None and obj is not None:
                self.facts.endpoints.append(
                    {
                        "file": self.path,
                        "line": node.lineno,
                        "line_end": node.end_lineno or node.lineno,
                        "symbol": qual,
                        "method": name.upper(),
                        "path": first,
                        "framework": obj,
                    }
                )
            elif name in ("route", "api_route") and first is not None:
                methods = ["GET"]
                for kw in deco.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, ast.List | ast.Tuple):
                        methods = [m.upper() for m in (_const_str(e) for e in kw.value.elts) if m]
                for method in methods:
                    self.facts.endpoints.append(
                        {
                            "file": self.path,
                            "line": node.lineno,
                            "line_end": node.end_lineno or node.lineno,
                            "symbol": qual,
                            "method": method,
                            "path": first,
                            "framework": obj or name,
                        }
                    )
            elif name in SUBSCRIBE_NAMES and first is not None:
                self.facts.topics.append(
                    {"file": self.path, "line": node.lineno, "symbol": qual, "topic": first, "direction": "SUBSCRIBES"}
                )
        self.scope.append(node.name)
        self.kinds.append("function")
        self.generic_visit(node)
        self.scope.pop()
        self.kinds.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        value = _const_str(node.value)
        if value is not None and not self.scope:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.facts.constants.setdefault(self.path, {})[target.id] = value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        obj, name = _call_name(node.func)
        if obj is None and name in self.bound and self.kinds and self.kinds[-1] == "function":
            module, symbol = self.bound[name]
            self.facts.import_calls.append(
                {"file": self.path, "line": node.lineno, "symbol": self._symbol(), "module": module, "name": symbol}
            )
        if name in HTTP_METHODS and obj in CLIENT_NAMES and node.args:
            url = url_template(node.args[0])
            if url is not None:
                self.facts.http_calls.append(
                    {
                        "file": self.path,
                        "line": node.lineno,
                        "symbol": self._symbol(),
                        "method": name.upper(),
                        "url": url,
                    }
                )
        elif name in PUBLISH_NAMES and node.args:
            topic = _const_str(node.args[0])
            if topic:
                self.facts.topics.append(
                    {
                        "file": self.path,
                        "line": node.lineno,
                        "symbol": self._symbol(),
                        "topic": topic,
                        "direction": "PUBLISHES",
                    }
                )
        elif name in SUBSCRIBE_NAMES and node.args and id(node) not in self.decorators:
            topic = _const_str(node.args[0])
            if topic and self.kinds and self.kinds[-1] == "function":
                self.facts.topics.append(
                    {
                        "file": self.path,
                        "line": node.lineno,
                        "symbol": self._symbol(),
                        "topic": topic,
                        "direction": "SUBSCRIBES",
                    }
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if isinstance(node.value, str) and SQL_HINT.search(node.value):
            sql_tables(self.facts, self.path, node.lineno, self._symbol(), node.value)


def sql_tables(facts: RepoFacts, path: str, line: int, symbol: str | None, sql: str) -> None:
    written = {m.group(1).lower() for m in SQL_WRITE.finditer(sql)}
    for table in sorted(written):
        facts.tables.append({"file": path, "line": line, "symbol": symbol, "table": table, "access": "WRITES"})
    if re.search(r"\bSELECT\b", sql, re.I):
        for table in sorted({m.group(1).lower() for m in SQL_READ.finditer(sql)} - written):
            facts.tables.append({"file": path, "line": line, "symbol": symbol, "table": table, "access": "READS"})


TS_CONST = re.compile(r"""(?:export\s+)?const\s+([A-Z_][A-Z0-9_]*)\s*=\s*["'`]([^"'`]+)["'`]""")
TS_FETCH = re.compile(r"""\bfetch\(\s*([`"'])(.*?)\1(.*?)\)""")
TS_AXIOS = re.compile(r"""\b(?:axios|http|client)\.(get|post|put|patch|delete)\(\s*([`"'])(.*?)\2""")
TS_METHOD = re.compile(r"""method\s*:\s*["'](GET|POST|PUT|PATCH|DELETE)["']""", re.I)
TS_ROUTE = re.compile(r"""\b(?:app|router)\.(get|post|put|patch|delete)\(\s*["'`]([^"'`]+)["'`]""")
TS_TOPIC = re.compile(r"""\b(publish|subscribe)\(\s*["'`]([^"'`]+)["'`]""")


def _ts_url(raw: str) -> str:
    return re.sub(r"\$\{\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\}", lambda m: "{" + m.group(1).split(".")[-1] + "}", raw)


def ts_facts(path: str, text: str, facts: RepoFacts) -> None:
    for i, line in enumerate(text.splitlines(), start=1):
        for m in TS_CONST.finditer(line):
            facts.constants.setdefault(path, {})[m.group(1)] = m.group(2)
        for m in TS_FETCH.finditer(line):
            method = TS_METHOD.search(m.group(3))
            facts.http_calls.append(
                {
                    "file": path,
                    "line": i,
                    "symbol": None,
                    "method": (method.group(1) if method else "GET").upper(),
                    "url": _ts_url(m.group(2)),
                }
            )
        for m in TS_AXIOS.finditer(line):
            facts.http_calls.append(
                {"file": path, "line": i, "symbol": None, "method": m.group(1).upper(), "url": _ts_url(m.group(3))}
            )
        for m in TS_ROUTE.finditer(line):
            facts.endpoints.append(
                {
                    "file": path,
                    "line": i,
                    "line_end": i,
                    "symbol": None,
                    "method": m.group(1).upper(),
                    "path": m.group(2),
                    "framework": "express",
                }
            )
        for m in TS_TOPIC.finditer(line):
            direction = "PUBLISHES" if m.group(1) == "publish" else "SUBSCRIBES"
            facts.topics.append({"file": path, "line": i, "symbol": None, "topic": m.group(2), "direction": direction})


def _dep_name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", spec.strip(), maxsplit=1)[0].lower()


def _dep_lines(text: str, deps: list[str]) -> dict[str, int]:
    lines = text.splitlines()
    out: dict[str, int] = {}
    for dep in deps:
        pattern = re.compile(r"""["'\s<]""" + re.escape(dep) + r"""(?=["'\s<>=!~;\[@/]|$)""", re.I)
        for i, line in enumerate(lines, start=1):
            if pattern.search(" " + line):
                out[dep] = i
                break
    return out


def manifest_facts(path: str, text: str, facts: RepoFacts) -> None:
    before = len(facts.manifests)
    _manifest(path, text, facts)
    for m in facts.manifests[before:]:
        m["dep_lines"] = _dep_lines(text, m["deps"])
        m["line"] = 1


def _manifest(path: str, text: str, facts: RepoFacts) -> None:
    name = path.rsplit("/", 1)[-1]
    try:
        if name == "pyproject.toml":
            project = tomllib.loads(text).get("project", {})
            if project.get("name"):
                facts.manifests.append(
                    {
                        "file": path,
                        "ecosystem": "pypi",
                        "name": str(project["name"]).lower(),
                        "version": str(project.get("version", "")),
                        "deps": [_dep_name(d) for d in project.get("dependencies", [])],
                        "dep_specs": {_dep_name(d): d for d in project.get("dependencies", [])},
                    }
                )
        elif name == "package.json":
            data = json.loads(text)
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if data.get("name"):
                facts.manifests.append(
                    {
                        "file": path,
                        "ecosystem": "npm",
                        "name": str(data["name"]).lower(),
                        "version": str(data.get("version", "")),
                        "deps": [d.lower() for d in deps],
                        "dep_specs": {d.lower(): str(v) for d, v in deps.items()},
                    }
                )
        elif name == "go.mod":
            module = re.search(r"^module\s+(\S+)", text, re.M)
            requires = re.findall(r"^\s*([\w\.\-/]+)\s+v[\w\.\-+]+", text, re.M)
            if module:
                facts.manifests.append(
                    {
                        "file": path,
                        "ecosystem": "go",
                        "name": module.group(1).lower(),
                        "version": "",
                        "deps": [r.lower() for r in requires],
                        "dep_specs": {},
                    }
                )
        elif name == "pom.xml":
            artifact = re.search(r"<artifactId>([^<]+)</artifactId>", text)
            pom_deps = re.findall(r"<dependency>.*?<artifactId>([^<]+)</artifactId>", text, re.S)
            if artifact:
                facts.manifests.append(
                    {
                        "file": path,
                        "ecosystem": "maven",
                        "name": artifact.group(1).lower(),
                        "version": "",
                        "deps": [d.lower() for d in pom_deps],
                        "dep_specs": {},
                    }
                )
    except (ValueError, tomllib.TOMLDecodeError):
        return


def repo_facts(files: dict[str, str]) -> RepoFacts:
    facts = RepoFacts()
    for path, text in sorted(files.items()):
        manifest_facts(path, text, facts)
        if path.endswith(".py"):
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError:
                continue
            _PyFacts(path, facts).visit(tree)
        elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
            ts_facts(path, text, facts)
        elif path.endswith(".sql"):
            for i, line in enumerate(text.splitlines(), start=1):
                if SQL_HINT.search(line):
                    sql_tables(facts, path, i, None, line)
    return facts
