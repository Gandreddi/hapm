"""Checks on a proposed change before it can reach a pull request."""

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from hapm_codeintel import looks_secret, unified_diff
from hapm_config import SandboxConfig


@dataclass
class GuardrailResult:
    diff: str
    diff_lines: int
    blocked: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_test.py")


def check_change(before: dict[str, str], edits: dict[str, str | None], cfg: SandboxConfig) -> GuardrailResult:
    """edits maps path -> new content, or None for a deleted file."""
    diffs: list[str] = []
    blocked: list[str] = []
    flags: list[str] = []
    for path, new in sorted(edits.items()):
        if any(fnmatchcase(path, p) for p in cfg.denied_paths):
            blocked.append(f"{path} is in a denied path")
        old = before.get(path, "")
        diffs.append(unified_diff(path, old, new or ""))
        if new is not None and looks_secret(
            "\n".join(line for line in (new or "").splitlines() if line not in old.splitlines())
        ):
            blocked.append(f"{path} adds something that looks like a secret")
        if _is_test(path):
            if new is None:
                flags.append(f"deletes test file {path}")
            elif len(re.findall(r"\bassert", new)) < len(re.findall(r"\bassert", old)):
                flags.append(f"removes assertions from {path}")
    diff = "".join(diffs)
    changed = sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))
    if changed > cfg.max_diff_lines:
        blocked.append(f"diff has {changed} changed lines; the limit is {cfg.max_diff_lines}")
    return GuardrailResult(diff, changed, blocked, flags)
