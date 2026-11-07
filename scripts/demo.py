"""make demo: eval, submit as requester, wait at the gate, approve as approver, confirm the story."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from hapm_mcp_client.client import mcp_transport

from scripts import hapmctl

STATE = Path(__file__).resolve().parent.parent / ".demo" / "state.json"
TRACKER_MCP = "http://127.0.0.1:8101/mcp"
SERVICES = {
    "gateway": "http://127.0.0.1:8001",
    "orchestrator": hapmctl.ORCHESTRATOR,
    "audit": hapmctl.AUDIT,
    "eval": hapmctl.EVAL,
    "oneview": hapmctl.ONEVIEW,
    "oidc": hapmctl.OIDC,
    "mock-tracker": "http://127.0.0.1:8101",
    "mock-repo": "http://127.0.0.1:8102",
    "portal": "http://127.0.0.1:3000/api/health",
}


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def wait_healthy(timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    for name, url in SERVICES.items():
        target = url if url.endswith("/api/health") else f"{url}/readyz"
        while True:
            try:
                if httpx.get(target, timeout=3).status_code == 200:
                    print(f"    {name}: ready")
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"{name} is not ready at {target}; run make up first")
            time.sleep(2)


def timeline_shows_waiting(run_id: str, timeout: float = 60) -> dict:  # type: ignore[type-arg]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{hapmctl.ONEVIEW}/v1/runs/{run_id}/timeline", headers=hapmctl.auth("viewer"), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            proposed = [i for i in data["items"] if i["type"] == "approval.requested"]
            if data["summary"]["state"] == "waiting_approval" and proposed:
                return dict(proposed[-1]["data"])
        time.sleep(1)
    raise SystemExit("OneView did not show the run waiting for approval")


def tracker_titles() -> list[str]:
    _, output = asyncio.run(mcp_transport(TRACKER_MCP, "list_stories", {"limit": 100}, 15))
    return [s["title"] for s in output["stories"]]


def main() -> int:
    step(1, "Checking that every service is ready")
    wait_healthy()

    step(2, "Running the dummy_echo eval suite (gates autonomy)")
    result = (
        httpx.post(
            f"{hapmctl.EVAL}/v1/evals/run",
            json={"agent_id": "dummy_echo", "suite_id": "dummy_echo"},
            headers=hapmctl.auth("admin"),
            timeout=300,
        )
        .raise_for_status()
        .json()
    )
    print(
        f"    aggregate {result['aggregate']} (threshold {result['threshold']}), passed={result['passed']}, cases={len(result['cases'])}"
    )
    if not result["passed"]:
        print("    eval failed, so dummy_echo stays suggest_only; stopping")
        return 1

    title = f"Demo story {time.strftime('%Y%m%d-%H%M%S')}"
    step(3, f'Submitting a task to dummy_echo as "requester" (story title: {title})')
    sub = hapmctl.submit("requester", "The payments service release moved to Thursday after load tests passed.", title)
    print(f"    run {sub['run_id']} (autonomy {sub['autonomy']})")

    step(4, "Waiting at the approval gate; checking OneView-lite")
    proposed = timeline_shows_waiting(sub["run_id"])
    print(f"    OneView shows waiting_approval with proposed action: {json.dumps(proposed['proposed_action'])}")

    step(5, 'Approving as a different user, "approver"')
    approval = hapmctl.pending_approval(sub["run_id"])
    resp = hapmctl.decide(approval["id"], "approver", "approve", "approved by make demo")
    resp.raise_for_status()
    print(f"    {approval['id']} approved")

    step(6, "Waiting for the run to finish and checking the mock tracker")
    final = hapmctl.wait_for(sub["run_id"], {"succeeded"}, timeout=120)
    present = title in tracker_titles()
    print(f"    run state: {final['state']}; story in mock tracker: {present}")
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(
        json.dumps(
            {"run_id": sub["run_id"], "task_id": sub["task_id"], "title": title, "eval_id": result["eval_id"]}, indent=2
        ),
        encoding="utf-8",
    )
    print(f"\nDemo complete. Open http://127.0.0.1:3000/runs/{sub['run_id']} (sign in as viewer / viewer-dev).")
    return 0 if present else 1


if __name__ == "__main__":
    sys.exit(main())
