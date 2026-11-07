"""M6 DONE checks: suite scores, gating blocks autonomy, hand edits cannot bypass, online scoring."""

import time
from pathlib import Path

import httpx
from hapm_orchestrator.agents import AgentRegistry
from hapm_orchestrator.autonomy import effective_autonomy
from sqlalchemy import Engine, text

from scripts import hapmctl

ROOT = Path(__file__).resolve().parents[2]


def run_suite(suite: str) -> dict:  # type: ignore[type-arg]
    resp = httpx.post(
        f"{hapmctl.EVAL}/v1/evals/run",
        json={"agent_id": "dummy_echo", "suite_id": suite},
        headers=hapmctl.auth("admin"),
        timeout=300,
    )
    resp.raise_for_status()
    return dict(resp.json())


def effective() -> str:
    items = httpx.get(f"{hapmctl.ORCHESTRATOR}/v1/agents", headers=hapmctl.auth("viewer"), timeout=10).json()["items"]
    return str(next(a for a in items if a["id"] == "dummy_echo")["effective_autonomy"])


def test_suite_scores_and_meets_threshold(app_engine: Engine) -> None:
    result = run_suite("dummy_echo")
    assert len(result["cases"]) >= 5
    assert result["passed"] and result["aggregate"] >= result["threshold"], result
    judged = next(c for c in result["cases"] if c["id"] == "judged-faithful")
    assert any(ch["name"] == "judge" and ch["passed"] for ch in judged["checks"])
    with app_engine.connect() as conn:
        stories = conn.execute(
            text("SELECT count(*) FROM tracker_stories WHERE title IN ('Release summary','Revenue note')")
        ).scalar()
    assert stories == 0, "eval mode must dry-run write tools"
    assert effective() == "act_with_approval"


def test_failing_eval_blocks_autonomy_then_passing_restores() -> None:
    failed = run_suite("dummy_echo_strict")
    assert not failed["passed"]
    assert effective() == "suggest_only"
    sub = hapmctl.submit("requester", "Gated run should only suggest.", "Gated")
    final = hapmctl.wait_for(sub["run_id"], {"succeeded"})
    assert final["autonomy"] == "suggest_only" and final["output"]["tool_calls"][0]["status"] == "suggested"
    assert run_suite("dummy_echo")["passed"]
    assert effective() == "act_with_approval"


def test_hand_edited_yaml_cannot_bypass_gate(app_engine: Engine) -> None:
    agent = AgentRegistry(ROOT / "agents").load("dummy_echo")
    edited = agent.model_copy(update={"system_prompt": agent.system_prompt + " Be bold."})
    with app_engine.connect() as conn:
        assert effective_autonomy(conn, agent)[0].value == "act_with_approval"
        autonomy, reason = effective_autonomy(conn, edited)
    assert autonomy.value == "suggest_only" and "no eval" in reason


def test_live_runs_are_scored_online(app_engine: Engine) -> None:
    sub = hapmctl.submit("requester", "Online scoring check.", "Online")
    hapmctl.decide(hapmctl.pending_approval(sub["run_id"])["id"], "approver", "approve")
    hapmctl.wait_for(sub["run_id"], {"succeeded"})
    # The online score is written just after the terminal state, so allow a short delay.
    deadline = time.monotonic() + 30
    row = None
    while row is None and time.monotonic() < deadline:
        with app_engine.connect() as conn:
            row = conn.execute(
                text("SELECT aggregate, passed FROM eval_results WHERE kind = 'online' AND run_id = :r"),
                {"r": sub["run_id"]},
            ).first()
        if row is None:
            time.sleep(0.5)
    assert row is not None, "no online score recorded"
    assert row.passed and row.aggregate == 1.0


def test_eval_api_requires_admin() -> None:
    body = {"agent_id": "dummy_echo", "suite_id": "dummy_echo"}
    assert httpx.post(f"{hapmctl.EVAL}/v1/evals/run", json=body, timeout=10).status_code == 401
    assert (
        httpx.post(f"{hapmctl.EVAL}/v1/evals/run", json=body, headers=hapmctl.auth("viewer"), timeout=10).status_code
        == 403
    )
    assert (
        httpx.post(
            f"{hapmctl.EVAL}/v1/evals/run",
            json={**body, "suite_id": "missing"},
            headers=hapmctl.auth("admin"),
            timeout=10,
        ).status_code
        == 404
    )
