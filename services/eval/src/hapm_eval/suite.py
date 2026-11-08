"""Eval suite format (YAML) and loading."""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Judge(BaseModel):
    rubric: str = Field(min_length=1)
    alias: str = "judge"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    input: dict[str, str]  # values are strings; steps convert numbers
    expect: dict[str, Any] = Field(default_factory=dict)
    judge: bool = False


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str
    version: str = "1"
    threshold: float = Field(default=0.8, ge=0, le=1)
    judge: Judge | None = None
    cases: list[Case] = Field(min_length=1)
    online: dict[str, Any] = Field(default_factory=dict)


def load_suite(directory: Path, suite_id: str) -> Suite:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", suite_id):
        raise KeyError(suite_id)
    path = directory / f"{suite_id}.yaml"
    if not path.exists():
        raise KeyError(suite_id)
    return Suite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
