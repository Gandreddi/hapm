"""Findings: where code must change and why. Locations are verified against the snapshot."""

import hashlib
import uuid
from typing import Any

from pydantic import BaseModel, Field


def fingerprint(repo: str, path: str, rule: str, symbol: str | None, evidence: str) -> str:
    """Stable across commits: ignores line numbers so moved code keeps its fingerprint."""
    basis = "|".join([repo, path, rule, symbol or "", " ".join(evidence.split())])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


class Finding(BaseModel):
    id: str
    repo: str
    sha: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str | None = None
    rule: str
    kind: str
    severity: str
    confidence: float = Field(ge=0, le=1)
    fingerprint: str
    evidence: str
    proposed_change: str
    test_approach: str
    source: str  # analyzer | llm

    @classmethod
    def create(cls, **data: Any) -> "Finding":
        fp = fingerprint(data["repo"], data["path"], data["rule"], data.get("symbol"), data["evidence"])
        return cls(id=f"fnd_{uuid.uuid4().hex[:12]}", fingerprint=fp, **data)

    def location(self) -> str:
        return f"{self.repo}@{self.sha[:10]}:{self.path}:{self.start_line}-{self.end_line}"


def verify_location(finding: Finding, files: dict[str, str]) -> bool:
    """The cited file must exist at that commit and the line range must be inside it."""
    text = files.get(finding.path)
    if text is None:
        return False
    count = len(text.splitlines())
    return 1 <= finding.start_line <= finding.end_line <= count


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def dedupe(findings: list[Finding], known_fingerprints: set[str], limit: int) -> tuple[list[Finding], int]:
    """Drop repeats and already-ticketed findings, sort by severity then autofix-friendliness, cap."""
    seen: set[str] = set(known_fingerprints)
    unique: list[Finding] = []
    dropped = 0
    for f in findings:
        if f.fingerprint in seen:
            dropped += 1
            continue
        seen.add(f.fingerprint)
        unique.append(f)
    unique.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.path, f.start_line))
    return unique[:limit], dropped
