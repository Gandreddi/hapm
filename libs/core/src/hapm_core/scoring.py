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


def findings_metrics(truth_path: str, detected: list[dict[str, Any]]) -> dict[str, float]:
    """Precision, recall and location accuracy of findings against a ground-truth file.

    A detection matches a seeded defect when it cites the same file and its line range covers the
    defect's line. Location accuracy: of detections in a file that has seeded defects, the share whose
    line range is exactly right.
    """
    import yaml

    from hapm_core.settings import REPO_ROOT

    truth = yaml.safe_load((REPO_ROOT / truth_path).read_text(encoding="utf-8"))["defects"]
    matched_truth: set[int] = set()
    matched_detections = 0
    in_truth_files = 0
    for d in detected:
        hit = next(
            (
                i
                for i, t in enumerate(truth)
                if t["path"] == d["path"] and d["start_line"] <= t["line"] <= d["end_line"] and i not in matched_truth
            ),
            None,
        )
        if any(t["path"] == d["path"] for t in truth):
            in_truth_files += 1
        if hit is not None:
            matched_truth.add(hit)
            matched_detections += 1
    return {
        "detected": len(detected),
        "seeded": len(truth),
        "recall": round(len(matched_truth) / len(truth), 4) if truth else 0.0,
        "precision": round(matched_detections / len(detected), 4) if detected else 0.0,
        "location_accuracy": round(matched_detections / in_truth_files, 4) if in_truth_files else 0.0,
    }


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
    step_outputs: dict[str, Any] = output["outputs"] if isinstance(output.get("outputs"), dict) else {}
    made_tools = [c.get("tool") for c in output.get("tool_calls", [])]
    for tool in expect.get("tool_calls_include", []):
        checks.append(Check(f"calls:{tool}", tool in made_tools, json.dumps(sorted(set(filter(None, made_tools))))))
    for tool in expect.get("tool_calls_exclude", []):
        checks.append(
            Check(f"never_calls:{tool}", tool not in made_tools, "ok" if tool not in made_tools else "called")
        )
    if "tests_passed" in expect:
        passed = bool((step_outputs.get("implement") or {}).get("tests_passed"))
        checks.append(Check("tests_passed", passed == bool(expect["tests_passed"]), str(passed)))
    for path in expect.get("skipped_paths", []):
        skipped = (step_outputs.get("intake") or {}).get("skipped", {})
        checks.append(Check(f"skipped:{path}", path in skipped, str(skipped.get(path, "read"))))
    if "linked_path" in expect:
        linked = ((step_outputs.get("plan_work") or {}).get("finding") or {}).get("path")
        checks.append(Check("linked_path", linked == expect["linked_path"], str(linked)))
    if "plan_kind" in expect:
        kind = (step_outputs.get("plan_work") or {}).get("kind")
        checks.append(Check("plan_kind", kind == expect["plan_kind"], str(kind)))
    if "findings_truth" in expect:
        metrics = findings_metrics(
            expect["findings_truth"]["path"], (step_outputs.get("intake") or {}).get("all_detected", [])
        )
        spec = expect["findings_truth"]
        detail = json.dumps(metrics)
        checks.append(Check("recall", metrics["recall"] >= float(spec.get("min_recall", 0)), detail))
        checks.append(Check("precision", metrics["precision"] >= float(spec.get("min_precision", 0)), detail))
        checks.append(
            Check(
                "location_accuracy", metrics["location_accuracy"] >= float(spec.get("min_location_accuracy", 0)), detail
            )
        )
    if judge_score is not None:
        checks.append(Check("judge", judge_score >= 0.5, f"score {judge_score}"))
    score = sum(c.passed for c in checks) / len(checks) if checks else 0.0
    return {"score": round(score, 4), "checks": [asdict(c) for c in checks]}
