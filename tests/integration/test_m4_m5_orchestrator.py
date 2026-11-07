"""M4 and M5 DONE checks: runs through the API, approval gates, identity rules, worker restart."""

import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import Engine, text

from scripts import hapmctl
from tests.integration.conftest import compose


@pytest.fixture(scope="module", autouse=True)
def dummy_echo_eval_passed(app_engine: Engine) -> None:
    """M6 runs real evals; here a passing eval row lets dummy_echo act with approval."""
    from pathlib import Path

    from hapm_orchestrator.agents import AgentRegistry, config_hash

    agent = AgentRegistry(Path(__file__).resolve().parents[2] / "agents").load("dummy_echo")
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_results (id, kind, agent_id, suite_id, version, config_hash, aggregate, threshold, passed, cases) "
                "VALUES (:i, 'offline', 'dummy_echo', 'fixture', '1', :h, 1.0, 0.8, true, '[]'::jsonb)"
            ),
            {"i": f"eval_fixture_{uuid.uuid4().hex[:8]}", "h": config_hash(agent)},
        )


def title() -> str:
    return f"Story {uuid.uuid4().hex[:8]}"


def stories(engine: Engine, story_title: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT count(*) FROM tracker_stories WHERE title = :t"), {"t": story_title}).scalar_one()
        )


def test_approved_run_succeeds_with_one_story_and_full_audit(app_engine: Engine) -> None:
    t = title()
    sub = hapmctl.submit("requester", "The quarterly report shows revenue grew by ten percent.", t)
    assert sub["autonomy"] == "act_with_approval"
    approval = hapmctl.pending_approval(sub["run_id"])
    assert approval["proposed_action"]["tool"] == "create_story"
    assert hapmctl.run(sub["run_id"])["state"] == "waiting_approval"
    assert hapmctl.decide(approval["id"], "approver", "approve", "looks fine").status_code == 200
    final = hapmctl.wait_for(sub["run_id"], {"succeeded"})
    assert final["output"]["summary"].startswith("Summary: The quarterly report")
    assert [c["status"] for c in final["output"]["tool_calls"]] == ["ok"]
    assert stories(app_engine, t) == 1
    with app_engine.connect() as conn:
        actions = set(
            conn.execute(text("SELECT action FROM audit_log WHERE run_id = :r"), {"r": sub["run_id"]}).scalars()
        )
    assert {
        "task.submit",
        "policy.llm.chat",
        "llm.chat.completed",
        "policy.memory.write",
        "memory.write.completed",
        "policy.tool.call",
        "approval.request",
        "approval.decide",
        "tool.call.completed",
        "run.finish",
    } <= actions


def test_rejected_run_creates_no_story() -> None:
    t = title()
    sub = hapmctl.submit("requester", "Reject me please.", t)
    approval = hapmctl.pending_approval(sub["run_id"])
    assert hapmctl.decide(approval["id"], "approver", "reject", "no").status_code == 200
    final = hapmctl.wait_for(sub["run_id"], {"rejected"})
    assert final["approvals"][0]["status"] == "rejected"


def test_rejected_run_has_no_story(app_engine: Engine) -> None:
    t = title()
    sub = hapmctl.submit("requester", "Another rejection.", t)
    hapmctl.decide(hapmctl.pending_approval(sub["run_id"])["id"], "approver", "reject")
    hapmctl.wait_for(sub["run_id"], {"rejected"})
    assert stories(app_engine, t) == 0


def test_edit_and_approve_uses_edited_args(app_engine: Engine) -> None:
    t, edited = title(), title()
    sub = hapmctl.submit("requester", "Edit this one.", t)
    approval = hapmctl.pending_approval(sub["run_id"])
    assert hapmctl.decide(approval["id"], "approver", "edit", "better title", {"title": edited}).status_code == 200
    hapmctl.wait_for(sub["run_id"], {"succeeded"})
    assert stories(app_engine, t) == 0 and stories(app_engine, edited) == 1
    bad = hapmctl.submit("requester", "Bad edit.", title())
    bad_approval = hapmctl.pending_approval(bad["run_id"])
    assert hapmctl.decide(bad_approval["id"], "approver", "edit", "", {"unknown": "x"}).status_code == 422
    hapmctl.decide(bad_approval["id"], "approver", "reject")


def test_identity_rules(app_engine: Engine) -> None:
    sub = hapmctl.submit("approver", "Self approval test.", title())
    approval = hapmctl.pending_approval(sub["run_id"])
    self_resp = hapmctl.decide(approval["id"], "approver", "approve")
    assert self_resp.status_code == 403 and "own task" in self_resp.text
    assert hapmctl.decide(approval["id"], "viewer", "approve").status_code == 403
    no_token = httpx.post(
        f"{hapmctl.ORCHESTRATOR}/v1/approvals/{approval['id']}/decision", json={"decision": "approve"}, timeout=10
    )
    assert no_token.status_code == 401
    forged = httpx.get(
        f"{hapmctl.ORCHESTRATOR}/v1/runs/{sub['run_id']}", headers={"Authorization": "Bearer not.a.jwt"}, timeout=10
    )
    assert forged.status_code == 401
    assert hapmctl.decide(approval["id"], "approver2", "approve").status_code == 200
    hapmctl.wait_for(sub["run_id"], {"succeeded"})
    with app_engine.connect() as conn:
        denied = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE resource = :a AND action = 'approval.decide' AND decision = 'deny'"
            ),
            {"a": approval["id"]},
        ).scalar()
    assert denied == 1


def test_timeout_ends_as_rejected_never_approved(app_engine: Engine) -> None:
    t = title()
    sub = hapmctl.submit("requester", "Nobody answers.", t, approval_timeout_seconds=3)
    final = hapmctl.wait_for(sub["run_id"], {"rejected"}, timeout=60)
    assert final["error"] == "approval timed out" and final["approvals"][0]["status"] == "expired"
    assert stories(app_engine, t) == 0


def test_cancel_while_waiting_and_cancel_approve_race(app_engine: Engine) -> None:
    t = title()
    sub = hapmctl.submit("requester", "Cancel me.", t)
    approval = hapmctl.pending_approval(sub["run_id"])
    assert hapmctl.cancel(sub["run_id"], "requester").status_code == 202
    late = hapmctl.decide(approval["id"], "approver", "approve")
    final = hapmctl.wait_for(sub["run_id"], {"cancelled", "succeeded"})
    time.sleep(2)
    again = hapmctl.run(sub["run_id"])
    assert again["state"] == final["state"]  # exactly one terminal state, never changed afterwards
    if final["state"] == "cancelled":
        assert late.status_code in (200, 409) and stories(app_engine, t) == 0
    with app_engine.connect() as conn:
        finishes = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE run_id = :r AND action = 'run.finish'"), {"r": sub["run_id"]}
        ).scalar()
    assert finishes == 1


def test_worker_restart_mid_run_still_completes(app_engine: Engine) -> None:
    t = title()
    sub = hapmctl.submit("requester", "Survive a worker crash during the LLM step.", t, fake_delay_seconds=8)
    hapmctl.wait_for(sub["run_id"], {"running"})
    time.sleep(2)
    compose("kill", "orchestrator-worker")
    time.sleep(2)
    compose("start", "orchestrator-worker")
    approval = hapmctl.pending_approval(sub["run_id"], timeout=120)
    compose("kill", "orchestrator-worker")
    assert hapmctl.decide(approval["id"], "approver", "approve").status_code == 200
    compose("start", "orchestrator-worker")
    hapmctl.wait_for(sub["run_id"], {"succeeded"}, timeout=180)
    assert stories(app_engine, t) == 1


def test_unknown_agent_and_bad_input() -> None:
    headers = hapmctl.auth("requester")
    r = httpx.post(
        f"{hapmctl.ORCHESTRATOR}/v1/tasks", json={"agent_id": "nope", "input": {}}, headers=headers, timeout=10
    )
    assert r.status_code == 404
    r = httpx.post(
        f"{hapmctl.ORCHESTRATOR}/v1/tasks",
        json={"agent_id": "dummy_echo", "input": {}, "options": {"x": 1}},
        headers=headers,
        timeout=10,
    )
    assert r.status_code == 422
    r = httpx.post(
        f"{hapmctl.ORCHESTRATOR}/v1/tasks",
        json={"agent_id": "dummy_echo", "input": {}, "mode": "eval"},
        headers=headers,
        timeout=10,
    )
    assert r.status_code == 403
    assert json.loads(httpx.get(f"{hapmctl.ORCHESTRATOR}/healthz", timeout=5).text)["status"] == "ok"
