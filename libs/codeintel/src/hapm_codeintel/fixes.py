"""Rule-based autofixes for findings that have a safe mechanical fix, plus unified diffs."""

import ast
import difflib
import re

from hapm_codeintel.findings import Finding


class FixError(ValueError):
    pass


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def _fix_mutable_default(text: str, finding: Finding) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(True)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == finding.symbol:
            args = node.args.args[len(node.args.args) - len(node.args.defaults) :]
            for arg, default in zip(args, node.args.defaults, strict=True):
                if isinstance(default, ast.List | ast.Dict | ast.Set) and default.lineno == finding.start_line:
                    empty = {ast.List: "[]", ast.Dict: "{}", ast.Set: "set()"}[type(default)]
                    header = lines[default.lineno - 1]
                    segment = ast.get_source_segment(text, default) or empty
                    lines[default.lineno - 1] = header.replace(f"{arg.arg}={segment}", f"{arg.arg}=None", 1)
                    first = node.body[0]
                    body_line = first.lineno - 1
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        body_line = first.end_lineno or first.lineno
                    indent = re.match(r"\s*", lines[node.body[0].lineno - 1]).group(0)  # type: ignore[union-attr]
                    guard = f"{indent}if {arg.arg} is None:\n{indent}    {arg.arg} = {empty}\n"
                    lines.insert(body_line, guard)
                    return "".join(lines)
    raise FixError("could not locate the mutable default")


def _replace_on_line(text: str, line_no: int, pattern: str, repl: str) -> str:
    lines = text.splitlines(True)
    new, count = re.subn(pattern, repl, lines[line_no - 1], count=1)
    if not count:
        raise FixError(f"pattern not found on line {line_no}")
    lines[line_no - 1] = new
    return "".join(lines)


def _first_match(text: str, line_no: int, options: list[tuple[str, str]]) -> str:
    for pattern, repl in options:
        try:
            return _replace_on_line(text, line_no, pattern, repl)
        except FixError:
            continue
    raise FixError(f"no known pattern on line {line_no}")


LITERAL_AHEAD = r"(?=\s*(['\"\d]))"


def apply_fix(text: str, finding: Finding) -> str:
    """Return the fixed file text, or raise FixError if the rule has no safe autofix here."""
    if finding.rule == "R1":
        fixed = _fix_mutable_default(text, finding)
    elif finding.rule == "R2":
        fixed = _first_match(text, finding.start_line, [(r"!=\s*None", "is not None"), (r"==\s*None", "is None")])
    elif finding.rule == "R3":
        fixed = _replace_on_line(text, finding.start_line, r"except\s*:", "except Exception:")
    elif finding.rule == "R8":
        fixed = _first_match(
            text, finding.start_line, [(r"\bis not\b" + LITERAL_AHEAD, "!="), (r"\bis\b" + LITERAL_AHEAD, "==")]
        )
    else:
        raise FixError(f"rule {finding.rule} has no autofix")
    ast.parse(fixed)  # never hand back code that does not parse
    return fixed
