"""Sentinel's only path to a model: the HAPM gateway (policy, budgets, redaction, usage records).

`llm: off` means no model call is ever made. The health check talks to a local OpenAI-compatible server
directly, only to verify it before a build: reachable, model id listed exactly, JSON output, embedding size.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from hapm_config import ProjectConfig, RepoConfig, resolve_ref
from hapm_config.loader import config_dir


class LlmDisabled(Exception):
    """Raised when a model call is attempted while llm is off."""


def effective_llm(project: ProjectConfig, repo: RepoConfig | None = None) -> str:
    return (repo.sentinel.llm if repo and repo.sentinel.llm else None) or project.sentinel.llm


@dataclass
class ChatReply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str


class GatewayLLM:
    def __init__(
        self, url: str | None = None, run_id: str | None = None, team: str = "sentinel", timeout: float = 180
    ) -> None:
        self.url = (url or os.environ.get("HAPM_GATEWAY_URL") or "http://127.0.0.1:8001").rstrip("/")
        self.run_id = run_id
        self.team = team
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"X-HAPM-Agent-Id": "sentinel", "X-HAPM-Team-Id": self.team}
        if self.run_id:
            headers["X-HAPM-Run-Id"] = self.run_id
        return headers

    def chat(
        self, alias: str, system: str, user: str, schema: dict[str, Any] | None = None, max_tokens: int = 600
    ) -> ChatReply:
        body: dict[str, Any] = {
            "model": alias,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "sentinel", "schema": schema, "strict": True},
            }
        resp = httpx.post(f"{self.url}/v1/chat", json=body, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        meta = data.get("hapm", {})
        return ChatReply(
            data["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            str(meta.get("provider", "")),
            str(meta.get("provider_model", "")),
        )

    def embed(self, alias: str, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{self.url}/v1/embeddings",
            json={"model": alias, "input": texts},
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [item["embedding"] for item in sorted(resp.json()["data"], key=lambda i: i["index"])]


def load_providers(directory: Path | None = None) -> dict[str, Any]:
    directory = directory or config_dir()
    for name in ("providers.yaml", "providers.example.yaml"):
        path = directory / name
        if path.exists():
            return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return {}


@dataclass
class HealthReport:
    mode: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))


def _targets(providers: dict[str, Any], alias: str) -> list[dict[str, Any]]:
    return list(providers.get("aliases", {}).get(alias, {}).get("targets", []))


def health_check(
    project: ProjectConfig, providers: dict[str, Any] | None = None, client: httpx.Client | None = None
) -> HealthReport:
    """Verify the model route Sentinel will use. Never prints secret values."""
    mode = project.sentinel.llm
    report = HealthReport(mode)
    if mode == "off":
        report.add("llm mode", True, "off: no model calls; enrichment, embeddings and sentinel_ask are disabled")
        return report
    providers = providers if providers is not None else load_providers()
    report.add(
        "data approval", bool(providers.get("data_approval")), "providers.data_approval must name the office approval"
    )
    aliases = project.sentinel.aliases
    wanted = "local_openai" if mode == "local" else "azure_openai"
    http = client or httpx.Client(timeout=30)
    for role, alias in (("enrich", aliases.enrich), ("ask", aliases.ask), ("embed", aliases.embed)):
        targets = _targets(providers, alias)
        first = targets[0] if targets else None
        pcfg = providers.get("providers", {}).get(first["provider"], {}) if first else {}
        if not first or pcfg.get("type") != wanted:
            report.add(
                f"alias {alias}",
                False,
                f"first target must be a {wanted} provider (llm: {mode}); found {pcfg.get('type') or 'none'}",
            )
            continue
        report.add(f"alias {alias}", True, f"{first['provider']} / {first['model']}")
        if mode != "local":
            refs = [pcfg.get(k) for k in ("endpoint_ref", "api_key_ref") if pcfg.get(k)]
            missing = [r for r in refs if not resolve_ref(r, required=False)]
            report.add(
                f"{alias} credentials",
                not missing,
                "all references resolve" if not missing else f"unresolved: {missing}",
            )
            continue
        base = (
            resolve_ref(pcfg.get("base_url_ref"), required=False) if pcfg.get("base_url_ref") else pcfg.get("endpoint")
        )
        if not base:
            report.add(f"{alias} base url", False, f"{pcfg.get('base_url_ref')} is not set")
            continue
        base = str(base).rstrip("/")
        key = resolve_ref(pcfg["api_key_ref"], required=False) if pcfg.get("api_key_ref") else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            listed = http.get(f"{base}/models", headers=headers).raise_for_status().json()
        except (httpx.HTTPError, ValueError) as exc:
            report.add(f"{alias} reachable", False, f"{base}/models failed: {type(exc).__name__}")
            continue
        ids = [m.get("id") for m in listed.get("data", [])]
        report.add(
            f"{alias} model listed", first["model"] in ids, f"wanted {first['model']!r}; server offers {ids[:10]}"
        )
        if first["model"] not in ids:
            continue
        if role == "embed":
            try:
                data = (
                    http.post(
                        f"{base}/embeddings",
                        headers=headers,
                        json={"model": first["model"], "input": ["sentinel health"]},
                    )
                    .raise_for_status()
                    .json()
                )
                dim = len(data["data"][0]["embedding"])
                expected = project.sentinel.embeddings.dimension
                report.add(
                    f"{alias} embedding dimension",
                    expected in (None, dim),
                    f"model returns {dim}; configured {expected}",
                )
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                report.add(f"{alias} embeddings", False, f"failed: {type(exc).__name__}")
            continue
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        body: dict[str, Any] = {
            "model": first["model"],
            "messages": [{"role": "user", "content": 'Reply with the JSON object {"ok": true} and nothing else.'}],
            "max_tokens": 20,
            "temperature": 0,
        }
        if pcfg.get("json_mode", "json_schema") == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "health", "schema": schema, "strict": True},
            }
        elif pcfg.get("json_mode") == "json_object":
            body["response_format"] = {"type": "json_object"}
        try:
            data = (
                http.post(f"{base}/chat/completions", headers=headers, json=body, timeout=120).raise_for_status().json()
            )
            parsed = json.loads(data["choices"][0]["message"]["content"])
            report.add(
                f"{alias} JSON output", parsed.get("ok") is True, "schema-constrained test prompt returned valid JSON"
            )
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            report.add(f"{alias} JSON output", False, f"failed: {type(exc).__name__}")
    return report
