from pathlib import Path

import pytest
from hapm_policy import DEFAULT_DENY_RULE, Effect, PolicyEngine, PolicyRequest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine.from_file(ROOT / "policies" / "default.yaml")


def req(action: str, resource: str = "x", actor: str = "agent:dummy_echo", **attrs: str) -> PolicyRequest:
    return PolicyRequest(actor=actor, action=action, resource=resource, attributes=attrs)


def test_llm_allowed_for_agents(engine: PolicyEngine) -> None:
    d = engine.evaluate(req("llm.chat", "fast"))
    assert d.effect is Effect.ALLOW and d.rule_id == "llm-agents-allowed" and d.allowed


def test_write_tool_requires_approval_with_policy(engine: PolicyEngine) -> None:
    d = engine.evaluate(req("tool.call", "create_story", kind="write"))
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert d.approval_policy is not None and d.approval_policy.forbid_self_approval
    assert engine.approval_policy("default_write").approvers == ["approver", "admin"]


def test_eval_mode_write_is_dry_run_allowed(engine: PolicyEngine) -> None:
    d = engine.evaluate(req("tool.call", "create_story", kind="write", mode="eval"))
    assert d.rule_id == "tools-write-eval-dry-run"


def test_read_tool_allowed(engine: PolicyEngine) -> None:
    assert engine.evaluate(req("tool.call", "list_stories", kind="read")).allowed


def test_default_deny(engine: PolicyEngine) -> None:
    d = engine.evaluate(req("db.drop", actor="user:mallory"))
    assert d.effect is Effect.DENY and d.rule_id == DEFAULT_DENY_RULE
    assert engine.evaluate(req("memory.read", "team")).effect is Effect.DENY


def test_first_match_wins_and_explicit_deny() -> None:
    e = PolicyEngine.from_dict(
        {
            "rules": [
                {"id": "deny-x", "effect": "deny", "match": {"resource": ["secret*"]}},
                {"id": "allow-all", "effect": "allow"},
            ]
        }
    )
    assert e.evaluate(req("a", "secret-file")).rule_id == "deny-x"
    assert e.evaluate(req("a", "public")).rule_id == "allow-all"


def test_invalid_policies_rejected() -> None:
    with pytest.raises(ValueError, match="approval_policy"):
        PolicyEngine.from_dict({"rules": [{"id": "r", "effect": "require_approval"}]})
    with pytest.raises(ValueError, match="unique"):
        PolicyEngine.from_dict({"rules": [{"id": "r", "effect": "allow"}, {"id": "r", "effect": "deny"}]})
