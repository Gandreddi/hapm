"""Small HTTP client for the local stack, shared by make demo, make verify and integration tests."""

import os
import time
from typing import Any

import httpx

ORCHESTRATOR = os.environ.get("HAPM_ORCHESTRATOR_URL_HOST", "http://127.0.0.1:8002")
OIDC = os.environ.get("HAPM_OIDC_URL_HOST", "http://127.0.0.1:8006")
AUDIT = os.environ.get("HAPM_AUDIT_URL_HOST", "http://127.0.0.1:8003")
EVAL = os.environ.get("HAPM_EVAL_URL_HOST", "http://127.0.0.1:8004")
ONEVIEW = os.environ.get("HAPM_ONEVIEW_URL_HOST", "http://127.0.0.1:8005")

# Dev-only test identities from services/oidc_stub/users.yaml.
DEV_PASSWORDS = {
    "requester": "requester-dev",
    "approver": "approver-dev",
    "approver2": "approver2-dev",
    "admin": "admin-dev",
    "viewer": "viewer-dev",
}
TERMINAL = {"succeeded", "failed", "rejected", "cancelled"}


def token(user: str) -> str:
    resp = httpx.post(
        f"{OIDC}/token", data={"grant_type": "password", "username": user, "password": DEV_PASSWORDS[user]}, timeout=10
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user)}"}


def submit(
    user: str, text: str, title: str, *, agent: str = "dummy_echo", mode: str = "live", **options: float
) -> dict[str, Any]:
    body = {"agent_id": agent, "input": {"text": text, "title": title}, "options": options, "mode": mode}
    resp = httpx.post(f"{ORCHESTRATOR}/v1/tasks", json=body, headers=auth(user), timeout=30)
    resp.raise_for_status()
    return dict(resp.json())


def submit_task(
    user: str, agent: str, task_input: dict[str, Any], mode: str = "live", **options: float
) -> dict[str, Any]:
    body = {"agent_id": agent, "input": task_input, "options": options, "mode": mode}
    resp = httpx.post(f"{ORCHESTRATOR}/v1/tasks", json=body, headers=auth(user), timeout=30)
    resp.raise_for_status()
    return dict(resp.json())


def pending_for_step(run_id: str, step: str, timeout: float = 180) -> dict[str, Any]:
    """Wait for the pending approval of one gate (approval ids end with the step id)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = run(run_id)
        for a in current["approvals"]:
            if a["status"] == "pending" and a["id"].endswith(f"_{step}"):
                return dict(a)
        if current["state"] in TERMINAL:
            raise RuntimeError(f"run {run_id} ended as {current['state']} ({current.get('error')}) before gate {step}")
        time.sleep(1)
    raise TimeoutError(f"no pending approval for gate {step} on {run_id}")


def run(run_id: str, user: str = "viewer") -> dict[str, Any]:
    resp = httpx.get(f"{ORCHESTRATOR}/v1/runs/{run_id}", headers=auth(user), timeout=30)
    resp.raise_for_status()
    return dict(resp.json())


def wait_for(run_id: str, states: set[str], timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        current = run(run_id)
        if current["state"] in states:
            return current
        if current["state"] in TERMINAL:
            raise RuntimeError(f"run {run_id} ended as {current['state']} ({current.get('error')}), expected {states}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {run_id} still {current['state']} after {timeout}s, expected {states}")
        time.sleep(1)


def pending_approval(run_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        approvals = [a for a in run(run_id)["approvals"] if a["status"] == "pending"]
        if approvals:
            return approvals[0]
        time.sleep(1)
    raise TimeoutError(f"no pending approval for {run_id}")


def decide(
    approval_id: str, user: str, decision: str, comment: str = "", edited_args: dict[str, str] | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"decision": decision, "comment": comment}
    if edited_args:
        body["edited_args"] = edited_args
    return httpx.post(f"{ORCHESTRATOR}/v1/approvals/{approval_id}/decision", json=body, headers=auth(user), timeout=30)


def cancel(run_id: str, user: str) -> httpx.Response:
    return httpx.post(f"{ORCHESTRATOR}/v1/runs/{run_id}/cancel", headers=auth(user), timeout=30)
