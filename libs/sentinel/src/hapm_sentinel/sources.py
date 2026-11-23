"""Where Sentinel reads repositories from: the GitHub MCP connector (normal path) or a fixture folder (offline).

Both return a codeintel Snapshot, so excluded, oversized, non-text and secret-looking files are dropped
before any engine, linker or model sees them. Sentinel only ever calls read tools.
"""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from hapm_codeintel.snapshot import Snapshot, blob_sha, is_excluded
from hapm_config import RepoConfig

REPO_ROOT = Path(__file__).resolve().parents[4]
READ_TOOLS = ("github_head", "github_list_tree", "github_read_file", "github_list_commits")


@dataclass
class RepoSource:
    snapshot: Snapshot
    commits: list[dict[str, Any]] = field(default_factory=list)


class SourceReader(Protocol):
    def head(self, repo: RepoConfig) -> str: ...

    def read(self, repo: RepoConfig, sha: str | None = None) -> RepoSource: ...


def fixture_head(files: dict[str, str]) -> str:
    """The same SHA the mock GitHub backend reports, so local and connector reads agree."""
    listing = json.dumps(sorted((p, blob_sha(t)) for p, t in files.items()))
    return hashlib.sha1(listing.encode(), usedforsecurity=False).hexdigest()


class LocalReader:
    """Reads a repository's mock_path folder directly. Offline CLI, tests and the synthetic fleet."""

    def __init__(self, root: Path = REPO_ROOT) -> None:
        self.root = root

    def _folder(self, repo: RepoConfig) -> Path:
        if not repo.mock_path:
            raise ValueError(f"repository {repo.name} has no mock_path; use the connector reader")
        return (self.root / repo.mock_path).resolve()

    def _files(self, repo: RepoConfig) -> dict[str, str]:
        """Every file, exactly as the mock GitHub backend serves them, so both report the same SHA.

        Excluded and secret-looking files are dropped afterwards by Snapshot.build: hashed, never kept.
        """
        base = self._folder(repo)
        files: dict[str, str] = {}
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            try:
                files[p.relative_to(base).as_posix()] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return files

    def head(self, repo: RepoConfig) -> str:
        return fixture_head(self._files(repo))

    def commits(self, repo: RepoConfig) -> list[dict[str, Any]]:
        sidecar = self._folder(repo).with_name(self._folder(repo).name + ".commits.yaml")
        if not sidecar.exists():
            return []
        return list(yaml.safe_load(sidecar.read_text(encoding="utf-8")) or [])

    def read(self, repo: RepoConfig, sha: str | None = None) -> RepoSource:
        files = self._files(repo)
        head = fixture_head(files)
        if sha and sha != head:
            raise ValueError(f"fixture {repo.name} is at {head}, not {sha}")
        snap = Snapshot.build(repo.name, head, files, repo.excluded_paths, repo.max_file_bytes)
        return RepoSource(snap, self.commits(repo))  # excluded files were hashed for the SHA, never kept


def _call(url: str, tool: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
    from hapm_mcp_client.client import mcp_transport

    _, output = asyncio.run(mcp_transport(url, tool, args, timeout))
    return dict(output)


class ConnectorReader:
    """Reads through the GitHub MCP connector with read tools only (never push or PR tools)."""

    def __init__(self, url: str | None = None, timeout: float = 30) -> None:
        self.url: str = url or os.environ.get("HAPM_GITHUB_MCP_URL") or "http://127.0.0.1:8104/mcp"
        self.timeout = timeout

    def _tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in READ_TOOLS:
            raise PermissionError(f"Sentinel is read-only; {tool} is not a read tool")
        return _call(self.url, tool, args, self.timeout)

    def head(self, repo: RepoConfig) -> str:
        return str(self._tool("github_head", {"repo": repo.name})["sha"])

    def read(self, repo: RepoConfig, sha: str | None = None) -> RepoSource:
        sha = sha or self.head(repo)
        tree = self._tool("github_list_tree", {"repo": repo.name, "ref": sha})["files"]
        files: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for entry in tree:
            path = entry["path"]
            if is_excluded(path, repo.excluded_paths):
                skipped[path] = "excluded"
            elif entry.get("size", 0) > repo.max_file_bytes:
                skipped[path] = "too large"
            else:
                files[path] = self._tool("github_read_file", {"repo": repo.name, "path": path, "ref": sha})["content"]
        snap = Snapshot.build(repo.name, sha, files, repo.excluded_paths, repo.max_file_bytes)
        snap.skipped.update(skipped)
        commits = self._tool("github_list_commits", {"repo": repo.name, "limit": 50}).get("commits", [])
        return RepoSource(snap, list(commits))


def reader_for(kind: str) -> SourceReader:
    if kind == "local":
        return LocalReader()
    if kind == "connector":
        return ConnectorReader()
    raise ValueError(f"unknown source reader {kind}")
