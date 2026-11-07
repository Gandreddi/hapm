"""M7 DONE checks: OneView timeline is complete, consumers dedupe, SSE pushes live events."""

import json
import threading
import time
import uuid

import httpx
from hapm_oneview.main import store_event
from sqlalchemy import Engine

from scripts import hapmctl


def timeline(run_id: str, timeout: float = 60, until_state: str | None = None) -> dict:  # type: ignore[type-arg]
    deadline = time.monotonic() + timeout
    while True:
        resp = httpx.get(f"{hapmctl.ONEVIEW}/v1/runs/{run_id}/timeline", headers=hapmctl.auth("viewer"), timeout=10)
        if resp.status_code == 200 and (until_state is None or resp.json()["summary"]["state"] == until_state):
            return dict(resp.json())
        if time.monotonic() > deadline:
            raise TimeoutError(f"timeline for {run_id}: {resp.status_code} {resp.text[:200]}")
        time.sleep(1)


def test_timeline_shows_every_step_of_a_run() -> None:
    sub = hapmctl.submit("requester", "OneView timeline check.", f"OV {uuid.uuid4().hex[:6]}")
    waiting = timeline(sub["run_id"], until_state="waiting_approval")
    proposed = [i for i in waiting["items"] if i["type"] == "approval.requested"]
    assert proposed and proposed[0]["data"]["proposed_action"]["tool"] == "create_story"
    hapmctl.decide(hapmctl.pending_approval(sub["run_id"])["id"], "approver", "approve")
    done = timeline(sub["run_id"], until_state="succeeded")
    s = done["summary"]
    assert s["states"][0] == "queued" and "waiting_approval" in s["states"] and s["states"][-1] == "succeeded"
    assert s["llm_calls"] >= 2 and s["tool_calls"] >= 1 and s["memory_accesses"] >= 1
    assert s["policy_decisions"] >= 3 and s["approvals"] == 2 and s["audit_rows"] > 5 and s["cost_usd"] > 0
    assert s["steps"][0] == "plan" and "finish" in s["steps"]
    assert "run_id=" in s["audit_url"]
    runs = httpx.get(f"{hapmctl.ONEVIEW}/v1/runs", headers=hapmctl.auth("viewer"), timeout=10).json()["items"]
    row = next(r for r in runs if r["run_id"] == sub["run_id"])
    assert row["requester"] == "requester" and row["state"] == "succeeded" and row["cost_usd"] > 0


def test_store_event_dedupes_by_event_id(app_engine: Engine) -> None:
    raw = json.dumps(
        {
            "id": str(uuid.uuid4()),
            "source": "itest",
            "type": "run.step",
            "subject": "run_dedupe",
            "data": {"run_id": "run_dedupe"},
            "unknown_field": True,
        }
    ).encode()
    assert store_event(app_engine, raw) is True
    assert store_event(app_engine, raw) is False


def test_oneview_requires_a_token_and_streams_live_events() -> None:
    assert httpx.get(f"{hapmctl.ONEVIEW}/v1/runs", timeout=10).status_code == 401
    received: list[dict] = []  # type: ignore[type-arg]

    def listen() -> None:
        with httpx.stream("GET", f"{hapmctl.ONEVIEW}/v1/stream", headers=hapmctl.auth("viewer"), timeout=30) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))
                    return

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(2)
    hapmctl.submit("requester", "SSE check.", "SSE")
    thread.join(timeout=30)
    assert received and received[0]["kind"] in {"state", "step", "policy", "llm", "memory", "tool", "approval"}
