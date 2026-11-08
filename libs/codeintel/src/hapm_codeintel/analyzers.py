"""Deterministic analysers. Each rule yields findings with an exact location and, when safe, an autofix id.

These cover Python. Other languages plug in by adding a function to ANALYZERS keyed by suffix.
"""

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass

from hapm_codeintel.findings import Finding
from hapm_codeintel.snapshot import Snapshot


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str  # bug | enhancement
    severity: str  # high | medium | low
    title: str
    proposed_change: str
    autofix: bool


RULES = {
    "R1": Rule(
        "R1",
        "bug",
        "high",
        "Mutable default argument",
        "Default to None and create a new object inside the function.",
        True,
    ),
    "R2": Rule("R2", "bug", "low", "Comparison to None with == or !=", "Use 'is None' or 'is not None'.", True),
    "R3": Rule(
        "R3", "bug", "medium", "Bare except", "Catch Exception (or a narrower type) instead of everything.", True
    ),
    "R4": Rule("R4", "enhancement", "low", "TODO or FIXME left in code", "Implement or track the noted work.", False),
    "R5": Rule(
        "R5",
        "bug",
        "medium",
        "assert used for runtime validation",
        "Raise a ValueError; asserts are removed with -O.",
        False,
    ),
    "R6": Rule("R6", "enhancement", "low", "print in library code", "Use the logging module.", False),
    "R7": Rule(
        "R7", "bug", "high", "Hard-coded credential", "Load the value from configuration or a secret store.", False
    ),
    "R8": Rule("R8", "bug", "medium", "Identity comparison with a literal", "Use == for value comparison.", True),
    "R9": Rule("R9", "bug", "medium", "Exception silently swallowed", "Log or re-raise instead of pass.", False),
}

CREDENTIAL_NAME = re.compile(r"(password|passwd|secret|api_key|token)$", re.I)
MARKER = re.compile(r"#\s*(TODO|FIXME)\b")


def _finding(
    snapshot: Snapshot, path: str, rule_id: str, line: int, end: int, symbol: str | None, evidence: str
) -> Finding:
    rule = RULES[rule_id]
    return Finding.create(
        repo=snapshot.repo,
        sha=snapshot.sha,
        path=path,
        start_line=line,
        end_line=end,
        symbol=symbol,
        rule=rule_id,
        kind=rule.kind,
        severity=rule.severity,
        confidence=0.9,
        evidence=evidence.strip()[:300],
        proposed_change=rule.proposed_change,
        test_approach="Run the repository tests; add a test that fails before the change and passes after.",
        source="analyzer",
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, snapshot: Snapshot, path: str, lines: list[str], is_test: bool) -> None:
        self.snapshot, self.path, self.lines, self.is_test = snapshot, path, lines, is_test
        self.found: list[Finding] = []
        self.func: list[str] = []

    def add(self, rule: str, node: ast.AST, symbol: str | None = None) -> None:
        line = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", line) or line
        self.found.append(
            _finding(
                self.snapshot,
                self.path,
                rule,
                line,
                end,
                symbol or (self.func[-1] if self.func else None),
                self.lines[line - 1] if line <= len(self.lines) else "",
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            if isinstance(default, ast.List | ast.Dict | ast.Set):
                self.add("R1", default, node.name)
        self.func.append(node.name)
        self.generic_visit(node)
        self.func.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.Eq | ast.NotEq) and isinstance(right, ast.Constant) and right.value is None:
                self.add("R2", node)
            if (
                isinstance(op, ast.Is | ast.IsNot)
                and isinstance(right, ast.Constant)
                and right.value is not None
                and not isinstance(right.value, bool)
            ):
                self.add("R8", node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add("R3", node)
        elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.add("R9", node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if not self.is_test:
            self.add("R5", node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if not self.is_test and isinstance(node.func, ast.Name) and node.func.id == "print":
            self.add("R6", node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value:
            for target in node.targets:
                if isinstance(target, ast.Name) and CREDENTIAL_NAME.search(target.id):
                    self.add("R7", node, target.id)
        self.generic_visit(node)


def analyze_python(snapshot: Snapshot, path: str) -> list[Finding]:
    text = snapshot.files[path]
    lines = text.splitlines()
    is_test = "/tests/" in f"/{path}" or path.rsplit("/", 1)[-1].startswith("test_")
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return []
    visitor = _Visitor(snapshot, path, lines, is_test)
    visitor.visit(tree)
    for number, line in enumerate(lines, start=1):
        if MARKER.search(line):
            visitor.found.append(_finding(snapshot, path, "R4", number, number, None, line))
    return sorted(visitor.found, key=lambda f: (f.start_line, f.rule))


ANALYZERS: dict[str, Callable[[Snapshot, str], list[Finding]]] = {".py": analyze_python}


def analyze_file(snapshot: Snapshot, path: str) -> list[Finding]:
    for suffix, fn in ANALYZERS.items():
        if path.endswith(suffix):
            return fn(snapshot, path)
    return []


def analyze_snapshot(snapshot: Snapshot) -> list[Finding]:
    found: list[Finding] = []
    for path in sorted(snapshot.files):
        found.extend(analyze_file(snapshot, path))
    return found
