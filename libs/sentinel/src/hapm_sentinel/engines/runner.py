"""Run graphify and SCIP indexers, or replay their recorded outputs.

SandboxRunner sends the snapshot (already filtered: no excluded, secret or oversized files) to the HAPM
sandbox service, which runs the pinned engine image as a throwaway container with no network, no
credentials, dropped capabilities and CPU, memory and time limits, and returns only the engine's JSON.
RecordedRunner replays outputs committed under tests/fixtures/sentinel/recorded so CI needs neither tool.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from hapm_codeintel.snapshot import Snapshot
from hapm_config import RepoConfig
from hapm_config.models import SentinelEngines

REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass
class EngineOutput:
    status: str  # ok | unavailable | failed | timeout | stale
    data: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    detail: str = ""


class EngineRunner(Protocol):
    name: str

    def graphify(self, repo: RepoConfig, snap: Snapshot) -> EngineOutput: ...

    def scip(self, repo: RepoConfig, snap: Snapshot, language: str, project: tuple[str, str]) -> EngineOutput: ...


def recorded_folder(base: Path, repo: RepoConfig) -> Path:
    for name in (repo.name, (repo.mock_path or "").rstrip("/").rsplit("/", 1)[-1]):
        if name and (base / name).is_dir():
            return base / name
    return base / repo.name


class RecordedRunner:
    name = "recorded"

    def __init__(self, base: Path) -> None:
        self.base = base if base.is_absolute() else REPO_ROOT / base

    def _load(self, repo: RepoConfig, snap: Snapshot, file: str) -> EngineOutput:
        folder = recorded_folder(self.base, repo)
        path = folder / file
        if not path.exists():
            return EngineOutput("unavailable", error=f"no recorded {file} for {repo.name}")
        meta_path = folder / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        recorded_sha = meta.get("sha")
        if recorded_sha and recorded_sha != snap.sha:
            return EngineOutput("stale", error=f"recording is for {recorded_sha[:12]}, snapshot is {snap.sha[:12]}")
        return EngineOutput("ok", json.loads(path.read_text(encoding="utf-8")), detail=f"recorded {path.name}")

    def graphify(self, repo: RepoConfig, snap: Snapshot) -> EngineOutput:
        return self._load(repo, snap, "graphify.json")

    def scip(self, repo: RepoConfig, snap: Snapshot, language: str, project: tuple[str, str]) -> EngineOutput:
        return self._load(repo, snap, f"scip-{language}.json")


class ServiceToken:
    """Client-credentials token from the identity provider, cached until shortly before expiry."""

    def __init__(self, token_url: str, client_id: str, client_secret: str) -> None:
        self.url, self.client_id, self.secret = token_url, client_id, client_secret
        self._token: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        if self._token is None or time.time() > self._expires - 60:
            resp = httpx.post(
                self.url,
                data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token, self._expires = str(data["access_token"]), time.time() + float(data.get("expires_in", 300))
        return self._token


class SandboxRunner:
    name = "sandbox"

    def __init__(self, engines: SentinelEngines, url: str | None = None, token: ServiceToken | None = None) -> None:
        self.engines = engines
        self.url = (url or os.environ.get("HAPM_SANDBOX_URL") or "http://127.0.0.1:8105").rstrip("/")
        self.token = token or ServiceToken(
            os.environ.get("HAPM_OIDC_TOKEN_URL", "http://127.0.0.1:8006/token"),
            os.environ.get("HAPM_SERVICE_CLIENT_ID", "eval-service"),
            os.environ.get("HAPM_SERVICE_CLIENT_SECRET", "eval-service-dev"),
        )

    def _run(
        self, image: str, command: str, files: dict[str, str], collect: str, timeout: int, memory_mb: int
    ) -> EngineOutput:
        if not files:
            return EngineOutput("unavailable", error="no files to index")
        body = {
            "image": image,
            "command": command,
            "files": files,
            "cpus": 2.0,
            "memory_mb": memory_mb,
            "pids_limit": 512,
            "timeout_seconds": timeout,
            "collect": collect,
        }
        started = time.monotonic()
        try:
            resp = httpx.post(
                f"{self.url}/v1/run",
                json=body,
                headers={"Authorization": f"Bearer {self.token()}"},
                timeout=timeout + 120,
            )
        except httpx.HTTPError as exc:
            return EngineOutput("unavailable", error=f"sandbox unreachable: {exc}")
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            return EngineOutput("failed", error=f"sandbox {resp.status_code}: {resp.text[:300]}", duration_ms=elapsed)
        result = resp.json()
        if result.get("timed_out"):
            return EngineOutput("timeout", error=f"engine exceeded {timeout}s", duration_ms=elapsed)
        artifact = result.get("artifact")
        if result.get("exit_code") != 0 or not artifact:
            return EngineOutput("failed", error=(result.get("output") or "no output")[-500:], duration_ms=elapsed)
        try:
            return EngineOutput("ok", json.loads(artifact), duration_ms=elapsed, detail=command)
        except json.JSONDecodeError as exc:
            return EngineOutput("failed", error=f"engine output is not JSON: {exc}", duration_ms=elapsed)

    def graphify(self, repo: RepoConfig, snap: Snapshot) -> EngineOutput:
        g = self.engines.graphify
        return self._run(g.image, g.command, dict(snap.files), g.output, g.timeout_seconds, g.memory_mb)

    def scip(self, repo: RepoConfig, snap: Snapshot, language: str, project: tuple[str, str]) -> EngineOutput:
        indexer = self.engines.scip.indexers.get(language)
        if indexer is None:
            return EngineOutput("unavailable", error=f"no SCIP indexer configured for {language}")
        name, version = project
        command = indexer.command.format(name=_shell_safe(name), version=_shell_safe(version or "0"))
        return self._run(
            indexer.image,
            command,
            dict(snap.files),
            self.engines.scip.output,
            self.engines.scip.timeout_seconds,
            indexer.memory_mb,
        )


def _shell_safe(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "._-")
    return cleaned or "project"


def runner_for(engines: SentinelEngines, kind: str | None = None) -> EngineRunner:
    kind = kind or engines.runner
    if kind == "recorded":
        return RecordedRunner(Path(engines.recorded_dir))
    if kind in ("docker", "sandbox"):
        return SandboxRunner(engines)
    raise ValueError(f"unknown engine runner {kind}")
