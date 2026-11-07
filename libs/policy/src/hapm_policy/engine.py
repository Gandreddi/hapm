"""Rules are checked in order; the first match wins; no match means deny."""

from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_DENY_RULE = "default-deny"


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approvers: list[str] = Field(min_length=1)
    forbid_self_approval: bool = True
    timeout_seconds: int = Field(default=3600, gt=0)


class Match(BaseModel):
    """Each field holds glob patterns; an empty list matches anything."""

    model_config = ConfigDict(extra="forbid")

    actor: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)
    resource: list[str] = Field(default_factory=list)
    attributes: dict[str, list[str]] = Field(default_factory=dict)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    effect: Effect
    match: Match = Field(default_factory=Match)
    approval_policy: str | None = None
    description: str = ""


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    approval_policies: dict[str, ApprovalPolicy] = Field(default_factory=dict)
    rules: list[Rule]

    @model_validator(mode="after")
    def check_references(self) -> "PolicyFile":
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        for rule in self.rules:
            if rule.effect is Effect.REQUIRE_APPROVAL and (rule.approval_policy not in self.approval_policies):
                raise ValueError(f"rule {rule.id} needs a known approval_policy")
        return self


class PolicyRequest(BaseModel):
    actor: str
    action: str
    resource: str
    attributes: dict[str, str] = Field(default_factory=dict)


class Decision(BaseModel):
    effect: Effect
    rule_id: str
    approval_policy_id: str | None = None
    approval_policy: ApprovalPolicy | None = None

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def _matches(patterns: list[str], value: str) -> bool:
    return not patterns or any(fnmatchcase(value, p) for p in patterns)


class PolicyEngine:
    def __init__(self, policy: PolicyFile) -> None:
        self._policy = policy

    @classmethod
    def from_file(cls, path: Path) -> "PolicyEngine":
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyEngine":
        return cls(PolicyFile.model_validate(data))

    def approval_policy(self, policy_id: str) -> ApprovalPolicy:
        return self._policy.approval_policies[policy_id]

    def evaluate(self, request: PolicyRequest) -> Decision:
        for rule in self._policy.rules:
            m = rule.match
            if (
                _matches(m.actor, request.actor)
                and _matches(m.action, request.action)
                and _matches(m.resource, request.resource)
                and all(_matches(patterns, request.attributes.get(key, "")) for key, patterns in m.attributes.items())
            ):
                approval = self._policy.approval_policies[rule.approval_policy] if rule.approval_policy else None
                return Decision(
                    effect=rule.effect,
                    rule_id=rule.id,
                    approval_policy_id=rule.approval_policy,
                    approval_policy=approval,
                )
        return Decision(effect=Effect.DENY, rule_id=DEFAULT_DENY_RULE)
