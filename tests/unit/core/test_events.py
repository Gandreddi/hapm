from pathlib import Path

import pytest
from hapm_core.context import CallerContext
from hapm_core.ids import new_id
from hapm_events import Event, SchemaRegistry, SchemaValidationError, make_event, topic_for

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry(ROOT / "schemas" / "events")


def test_every_event_type_has_a_topic(registry: SchemaRegistry) -> None:
    assert len(registry.types) == 10
    for event_type in registry.types:
        assert topic_for(event_type) in {"runs.events", "audit.events", "approvals.events", "llm.usage"}


def test_unknown_topic_rejected() -> None:
    with pytest.raises(ValueError):
        topic_for("billing.charged")


def test_validation_accepts_valid_and_extra_fields(registry: SchemaRegistry) -> None:
    registry.validate(
        "run.state_changed",
        {"run_id": "r", "task_id": "t", "agent_id": "a", "state": "running", "extra": 1},
    )


def test_validation_rejects_missing_and_unknown(registry: SchemaRegistry) -> None:
    with pytest.raises(SchemaValidationError, match="state"):
        registry.validate("run.state_changed", {"run_id": "r", "task_id": "t", "agent_id": "a"})
    with pytest.raises(SchemaValidationError, match="unknown"):
        registry.validate("run.unknown", {})


def test_empty_schema_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SchemaRegistry(tmp_path)


def test_consumers_ignore_unknown_envelope_fields() -> None:
    event = make_event("hapm/test", "run.step", {"a": 1}, subject="run_1")
    parsed = Event.model_validate({**event.model_dump(), "futurefield": "x"})
    assert parsed.id == event.id and parsed.schemaversion == 1


def test_caller_context_headers_and_validation() -> None:
    ctx = CallerContext(agent_id="dummy_echo", team_id="core", run_id="run_1")
    assert ctx.actor == "agent:dummy_echo"
    assert ctx.headers()["X-HAPM-Run-Id"] == "run_1"
    assert "X-HAPM-Task-Id" not in ctx.headers()
    with pytest.raises(ValueError):
        CallerContext(agent_id="Bad Agent!", team_id="core")
    assert new_id("run").startswith("run_")
