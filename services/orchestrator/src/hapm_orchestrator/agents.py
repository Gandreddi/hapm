"""Agents are YAML files validated by Pydantic. Adding an agent means adding a file, no code."""

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from hapm_core.hashing import sha256_hex
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Autonomy(StrEnum):
    SUGGEST_ONLY = "suggest_only"
    ACT_WITH_APPROVAL = "act_with_approval"


class LlmStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["llm"]
    description: str = ""
    prompt: str
    model_alias: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)


class MemoryWriteStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["memory_write"]
    description: str = ""
    scope: Literal["run", "task", "agent"]
    content: str
    ttl_seconds: int | None = Field(default=None, gt=0)


class ToolStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["tool"]
    description: str = ""
    tool: str
    args: dict[str, str]


PLUGIN_REF = r"^[a-z_][a-z0-9_.]*:[a-z_][a-z0-9_]*$"


class PluginStep(BaseModel):
    """Runs a Python function (module:function) that reaches LLM, memory and tools only via Core clients."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["plugin"]
    description: str = ""
    plugin: str = Field(pattern=PLUGIN_REF)
    config: dict[str, str] = Field(default_factory=dict)


class GateStep(BaseModel):
    """A human approval gate. The plugin builds the approval request, or returns None to skip the gate."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["gate"]
    description: str = ""
    plugin: str = Field(pattern=PLUGIN_REF)
    gate: str = Field(pattern=r"^[a-z_]+$")


Step = Annotated[LlmStep | MemoryWriteStep | ToolStep | PluginStep | GateStep, Field(discriminator="type")]


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    version: str = "1"
    description: str = ""
    team_id: str
    system_prompt: str
    model_alias: str = "fast"
    autonomy: Autonomy = Autonomy.SUGGEST_ONLY
    allowed_tools: list[str] = Field(default_factory=list)
    memory_scopes: list[Literal["run", "task", "agent"]] = Field(default_factory=list)
    approval_policy: str = "default_write"
    eval_suite: str | None = None
    eval_threshold: float = Field(default=0.8, ge=0, le=1)
    max_steps: int = Field(default=20, gt=0)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def check_steps(self) -> "AgentConfig":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        reserved = {"plan", "finish"} & set(ids)
        if reserved:
            raise ValueError(f"step ids {sorted(reserved)} are reserved by the workflow")
        if len(self.steps) > self.max_steps:
            raise ValueError("more steps than max_steps")
        for step in self.steps:
            if isinstance(step, ToolStep) and step.tool not in self.allowed_tools:
                raise ValueError(f"step {step.id} uses tool {step.tool} that is not in allowed_tools")
            if isinstance(step, MemoryWriteStep) and step.scope not in self.memory_scopes:
                raise ValueError(f"step {step.id} writes {step.scope} memory that is not in memory_scopes")
        return self


def config_hash(agent: AgentConfig) -> str:
    """Any change to the agent definition changes its hash, which resets autonomy gating."""
    return sha256_hex(json.dumps(agent.model_dump(mode="json"), sort_keys=True))


class AgentRegistry:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self, agent_id: str) -> AgentConfig:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", agent_id):
            raise KeyError(agent_id)
        path = self.directory / f"{agent_id}.yaml"
        if not path.exists():
            raise KeyError(agent_id)
        agent = AgentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if agent.id != agent_id:
            raise ValueError(f"{path.name} declares id {agent.id}")
        return agent

    def all(self) -> list[AgentConfig]:
        return [self.load(p.stem) for p in sorted(self.directory.glob("*.yaml"))]


_PLACEHOLDER = re.compile(r"\{(input|steps)\.([a-z][a-z0-9_]*)\}")


def render(template: str, values: dict[str, Any]) -> str:
    """Replace {input.x} and {steps.y} placeholders; unknown placeholders become empty."""

    def sub(match: re.Match[str]) -> str:
        group, key = match.groups()
        return str(values.get(group, {}).get(key, ""))

    return _PLACEHOLDER.sub(sub, template)


def write_schema(path: Path) -> None:
    path.write_text(json.dumps(AgentConfig.model_json_schema(), indent=2) + "\n", encoding="utf-8")
