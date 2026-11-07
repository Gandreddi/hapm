"""Scorers shared by offline evals and online scoring of real runs."""

import json
from dataclasses import asdict, dataclass
from typing import Any

from jsonschema import Draft202012Validator


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def _text(output: dict[str, Any]) -> str:
    return str(output.get("summary") or "")


def score_output(expect: dict[str, Any], output: dict[str, Any], judge_score: float | None = None) -> dict[str, Any]:
    """Score one output against expected properties. Score is the share of checks that passed."""
    checks: list[Check] = []
    text = _text(output)
    for needle in expect.get("contains", []):
        checks.append(Check(f"contains:{needle}", needle.lower() in text.lower(), text[:120]))
    for needle in expect.get("not_contains", []):
        checks.append(Check(f"not_contains:{needle}", needle.lower() not in text.lower(), text[:120]))
    if "json_schema" in expect:
        errors = list(Draft202012Validator(expect["json_schema"]).iter_errors(output))
        checks.append(Check("json_schema", not errors, errors[0].message if errors else "ok"))
    if "tool_calls" in expect:
        made = [c["tool"] for c in output.get("tool_calls", [])]
        checks.append(Check("tool_calls", made == list(expect["tool_calls"]), json.dumps(made)))
    if "max_steps" in expect:
        steps = int(output.get("steps", 0))
        checks.append(Check("max_steps", steps <= int(expect["max_steps"]), f"{steps} steps"))
    if "max_cost" in expect:
        cost = float(output.get("cost_usd", 0))
        checks.append(Check("max_cost", cost <= float(expect["max_cost"]), f"${cost:.6f}"))
    if judge_score is not None:
        checks.append(Check("judge", judge_score >= 0.5, f"score {judge_score}"))
    score = sum(c.passed for c in checks) / len(checks) if checks else 0.0
    return {"score": round(score, 4), "checks": [asdict(c) for c in checks]}
