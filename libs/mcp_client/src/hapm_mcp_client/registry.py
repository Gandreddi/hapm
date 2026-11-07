"""Tool registry loaded from connectors/tools.yaml."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServerSpec(BaseModel):
    url_env: str
    default_url: str

    @property
    def url(self) -> str:
        return os.environ.get(self.url_env, self.default_url)


class Defaults(BaseModel):
    timeout_seconds: float = Field(default=15, gt=0)
    retries: int = Field(default=2, ge=0, le=5)
    max_args_bytes: int = Field(default=16_384, gt=0)
    max_response_bytes: int = Field(default=65_536, gt=0)


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    server: str
    kind: Literal["read", "write"]
    timeout_seconds: float | None = None
    retries: int | None = None
    max_args_bytes: int | None = None
    max_response_bytes: int | None = None


class ToolRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: dict[str, ServerSpec]
    tools: list[ToolSpec]
    defaults: Defaults = Field(default_factory=Defaults)

    @model_validator(mode="after")
    def check(self) -> "ToolRegistry":
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        for tool in self.tools:
            if tool.server not in self.servers:
                raise ValueError(f"tool {tool.name} uses unknown server {tool.server}")
        return self

    def get(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def limit(self, tool: ToolSpec, field: str) -> float:
        value = getattr(tool, field)
        return float(value if value is not None else getattr(self.defaults, field))

    @classmethod
    def load(cls, path: Path) -> "ToolRegistry":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
