"""Typed gateway configuration loaded from config.yaml."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["fake", "azure_openai", "anthropic", "local_openai"]
    always_fail: bool = False
    endpoint: str | None = None
    endpoint_ref: str | None = None
    api_key_ref: str | None = None
    api_version: str | None = None
    # local_openai: any OpenAI-compatible server on the office machine (Ollama, llama.cpp llama-server, vLLM).
    server: Literal["ollama", "llama_cpp", "vllm"] | None = None  # informational; ports 11434, 8080, 8000
    base_url_ref: str | None = None  # e.g. env:SENTINEL_LLM_BASE_URL with a value like http://127.0.0.1:8080/v1
    quantization: str | None = None  # e.g. Q4_K_M; recorded with every Sentinel run
    context_tokens: int | None = Field(default=None, gt=0)
    max_concurrency: int = Field(default=1, ge=1, le=64)  # 1 for Ollama and llama.cpp, higher for vLLM
    timeout_seconds: float = Field(default=120, gt=0)
    json_mode: Literal["json_schema", "json_object", "none"] = "json_schema"
    # Deprecated spellings kept for older config files.
    endpoint_env: str | None = None
    api_key_env: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_retired(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("type") == "github_models":
            raise ValueError(
                "github_models is no longer supported: GitHub retired GitHub Models on 2026-07-30. "
                "Use azure_openai with Azure AI Foundry deployments (ADR-0020)."
            )
        return data


class Target(BaseModel):
    provider: str
    model: str


class AliasConfig(BaseModel):
    timeout_seconds: float = Field(default=30, gt=0)
    targets: list[Target] = Field(min_length=1)


class Price(BaseModel):
    prompt: float = Field(ge=0)
    completion: float = Field(ge=0)


class Limit(BaseModel):
    tokens: int = Field(gt=0)
    cost_usd: float = Field(gt=0)


class TeamBudget(BaseModel):
    per_run: Limit
    per_team_daily: Limit


class Budgets(TeamBudget):
    team_overrides: dict[str, TeamBudget] = Field(default_factory=dict)

    def for_team(self, team_id: str) -> TeamBudget:
        return self.team_overrides.get(team_id, self)


class Redaction(BaseModel):
    enabled: bool = True
    patterns: dict[str, str] = Field(default_factory=dict)


class Retention(BaseModel):
    usage_days: int = Field(default=90, gt=0)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_approval: str = ""
    providers: dict[str, ProviderConfig]
    aliases: dict[str, AliasConfig]
    prices: dict[str, Price]
    budgets: Budgets
    redaction: Redaction = Field(default_factory=Redaction)
    retention: Retention = Field(default_factory=Retention)

    @model_validator(mode="after")
    def check_targets(self) -> "GatewayConfig":
        if "default" not in self.prices:
            raise ValueError("prices.default is required")
        for name, alias in self.aliases.items():
            for target in alias.targets:
                if target.provider not in self.providers:
                    raise ValueError(f"alias {name} uses unknown provider {target.provider}")
        return self

    def price(self, model: str) -> Price:
        return self.prices.get(model, self.prices["default"])

    @classmethod
    def load(cls, path: Path) -> "GatewayConfig":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
