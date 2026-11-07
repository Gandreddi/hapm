"""Generate one JSON Schema per event type into schemas/events. Re-run after editing EVENTS."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STR = {"type": "string"}
OPT_STR = {"type": ["string", "null"]}
NUM = {"type": "number"}
INT = {"type": "integer"}
OBJ = {"type": "object"}

EVENTS: dict[str, dict[str, dict[str, object]]] = {
    "run.state_changed": {"run_id": STR, "task_id": STR, "agent_id": STR, "state": STR},
    "run.step": {"run_id": STR, "task_id": STR, "step": STR, "kind": STR, "status": STR},
    "run.tool_called": {"run_id": STR, "task_id": STR, "tool": STR, "kind": STR, "status": STR},
    "run.memory_accessed": {"run_id": STR, "operation": STR, "scope": STR, "allowed": {"type": "boolean"}},
    "run.policy_decided": {"run_id": OPT_STR, "action": STR, "resource": STR, "decision": STR, "rule_id": STR},
    "approval.requested": {"approval_id": STR, "run_id": STR, "task_id": STR, "what": STR, "proposed_action": OBJ},
    "approval.decided": {"approval_id": STR, "run_id": STR, "decision": STR, "decided_by": STR},
    "audit.recorded": {"audit_id": STR, "run_id": OPT_STR, "action": STR, "decision": STR},
    "llm.usage": {
        "run_id": OPT_STR,
        "agent_id": STR,
        "team_id": STR,
        "provider": STR,
        "model": STR,
        "prompt_tokens": INT,
        "completion_tokens": INT,
        "cost_usd": NUM,
        "latency_ms": NUM,
    },
    "eval.completed": {
        "eval_id": STR,
        "agent_id": STR,
        "suite_id": STR,
        "aggregate": NUM,
        "passed": {"type": "boolean"},
    },
}


def main() -> None:
    out = ROOT / "schemas" / "events"
    out.mkdir(parents=True, exist_ok=True)
    for event_type, props in EVENTS.items():
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://hapm.local/schemas/events/{event_type}.json",
            "title": event_type,
            "description": "Data payload of the event. Unknown extra fields are allowed.",
            "type": "object",
            "required": sorted(props),
            "properties": props,
            "additionalProperties": True,
        }
        (out / f"{event_type}.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(EVENTS)} schemas to {out}")


if __name__ == "__main__":
    main()
