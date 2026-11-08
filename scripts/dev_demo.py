"""make dev-demo: Developer agent offline demo (fixture repo, mock Jira and GitHub, Fake model, real sandbox).

Scan -> Gate 1 (approve one finding, reject the rest) -> ticket -> Gate 2 (plan) -> sandbox fix and tests
-> Gate 3 (diff and results) -> branch and pull request in the mock repository.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from hapm_mcp_client.client import mcp_transport

from scripts import hapmctl
from scripts.demo import wait_healthy

STATE = Path(__file__).resolve().parent.parent / ".demo" / "dev_state.json"
JIRA_MCP = "http://127.0.0.1:8103/mcp"
GITHUB_MCP = "http://127.0.0.1:8104/mcp"
REPO = "payments-service"


def mcp(url: str, tool: str, args: dict) -> dict:  # type: ignore[type-arg]
    _, output = asyncio.run(mcp_transport(url, tool, args, 20))
    return dict(output)


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def finding_ids(approval: dict) -> list[tuple[str, str]]:  # type: ignore[type-arg]
    """(finding id, preview line) pairs from the Gate 1 preview."""
    return [(line.split(" | ")[0], line) for line in approval["preview"].splitlines() if line.startswith("fnd_")]


def main() -> int:
    step(1, "Checking that every service is ready")
    wait_healthy()
    for name, url in (
        ("jira-connector", "http://127.0.0.1:8103"),
        ("github-connector", "http://127.0.0.1:8104"),
        ("sandbox", "http://127.0.0.1:8105"),
    ):
        httpx.get(f"{url}/readyz", timeout=5).raise_for_status()
        print(f"    {name}: ready")

    step(2, "Running the Developer agent eval suite (gates autonomy)")
    result = (
        httpx.post(
            f"{hapmctl.EVAL}/v1/evals/run",
            json={"agent_id": "developer", "suite_id": "developer"},
            headers=hapmctl.auth("admin"),
            timeout=600,
        )
        .raise_for_status()
        .json()
    )
    print(f"    aggregate {result['aggregate']} (threshold {result['threshold']}), passed={result['passed']}")
    scan = next(c for c in result["cases"] if c["id"] == "scan-finds-seeded-defects")
    print(f"    scan metrics (Fake model, analysers only): {scan['checks'][1]['detail']}")
    if not result["passed"]:
        return 1

    step(3, f'Submitting a repo_scan of {REPO} as "requester"')
    sub = hapmctl.submit_task(
        "requester",
        "developer",
        {"mode": "repo_scan", "repo": REPO, "max_tickets": "3", "hidden_tests": "payments_service"},
    )
    run_id = sub["run_id"]
    print(f"    run {run_id} (autonomy {sub['autonomy']})")

    step(4, "Gate 1: findings with exact locations")
    gate1 = hapmctl.pending_for_step(run_id, "approve_tickets")
    pairs = finding_ids(gate1)
    for _, line in pairs:
        print(f"    {line[:140]}")
    keep = next(fid for fid, line in pairs if "ledger.py:4-4" in line)
    print(f'    "approver" keeps only {keep} (add_entry mutable default) and rejects the others')
    hapmctl.decide(gate1["id"], "approver", "edit", "only the ledger bug", {"finding_ids": keep}).raise_for_status()

    step(5, "Gate 2: implementation plan")
    gate2 = hapmctl.pending_for_step(run_id, "approve_plan")
    print("    " + gate2["preview"].replace("\n", "\n    "))
    hapmctl.decide(gate2["id"], "approver", "approve", "plan looks right").raise_for_status()

    step(6, "Gate 3: diff and sandbox test results")
    gate3 = hapmctl.pending_for_step(run_id, "approve_pr")
    print("    " + gate3["preview"].replace("\n", "\n    "))
    hapmctl.decide(gate3["id"], "approver", "approve", "ship it").raise_for_status()

    step(7, "Waiting for the run to finish; checking mock Jira and mock GitHub")
    final = hapmctl.wait_for(run_id, {"succeeded"}, timeout=180)
    tickets = mcp(JIRA_MCP, "jira_search_issues", {"project": "PAY", "labels": ["hapm"], "limit": 50})["issues"]
    pr = final["output"]["outputs"]["open_pr"]
    prs = mcp(GITHUB_MCP, "github_list_issues_and_prs", {"repo": REPO})["pull_requests"]
    ticket = final["output"]["outputs"]["create_tickets"]["tickets"]
    print(f"    run state: {final['state']}")
    print(f"    tickets created by this run: {[t['key'] for t in ticket]} (Jira now holds {len(tickets)} hapm tickets)")
    print(f"    pull request: #{pr['pull_request']['number']} from {pr['branch']}; open PRs in mock repo: {len(prs)}")
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kept_finding": keep,
                "ticket": ticket[0]["key"],
                "branch": pr["branch"],
                "pr": pr["pull_request"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nDeveloper demo complete. Timeline: http://127.0.0.1:3000/runs/{run_id}")
    return 0 if final["state"] == "succeeded" and len(ticket) == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
