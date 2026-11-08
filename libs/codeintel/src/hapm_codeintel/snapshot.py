"""A repository snapshot at one commit: path -> text, plus the commit SHA it came from."""

import hashlib
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

SECRET_PATTERNS = [
    re.compile(r"sk_(live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
]
TEXT_SUFFIXES = (
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".ts",
    ".java",
    ".go",
    ".cs",
)


def looks_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def is_excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(path, p) for p in patterns)


def blob_sha(text: str) -> str:
    """Same hash Git uses for a blob, so real and mock snapshots agree on unchanged files."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass
class Snapshot:
    repo: str
    sha: str
    files: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)  # path -> reason

    @classmethod
    def build(cls, repo: str, sha: str, files: dict[str, str], excluded: list[str], max_bytes: int) -> "Snapshot":
        """Drop excluded, oversized, non-text and secret-looking files before anything sees them."""
        kept: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for path, text in sorted(files.items()):
            if is_excluded(path, excluded):
                skipped[path] = "excluded"
            elif not path.endswith(TEXT_SUFFIXES):
                skipped[path] = "not text"
            elif len(text.encode("utf-8")) > max_bytes:
                skipped[path] = "too large"
            elif looks_secret(text):
                skipped[path] = "secret pattern"
            else:
                kept[path] = text
        return cls(repo, sha, kept, skipped)

    def lines(self, path: str) -> list[str]:
        return self.files[path].splitlines()
