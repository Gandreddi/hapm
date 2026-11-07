"""make verify: assert each part of the Phase 0 exit criterion. Prints PASS/FAIL; exits 1 on any failure."""

import json
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from scripts import hapmctl
from scripts.demo import STATE, tracker_titles

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    return default


ENGINE = create_engine(
    f"postgresql+psycopg://hapm_readonly:{env('HAPM_READONLY_DB_PASSWORD', 'hapm-ro-local')}@127.0.0.1:5432/hapm"
)


def compose(*args: str) -> None:
    import subprocess

    cmd = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "deploy/compose/compose.yaml"),
        *args,
    ]
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603, S607 - fixed docker compose command


def check_a(state: dict) -> str:  # type: ignore[type-arg]
    run = hapmctl.run(state["run_id"])
    assert run["state"] == "succeeded", f"run state is {run['state']}"
    return f"run {state['run_id']} is succeeded"


def check_b(state: dict) -> str:  # type: ignore[type-arg]
    run_id = state["run_id"]
    with ENGINE.connect() as conn:
        actions = [r[0] for r in conn.execute(text("SELECT action FROM audit_log WHERE run_id = :r"), {"r": run_id})]
        llm_calls = conn.execute(text("SELECT count(*) FROM llm_usage WHERE run_id = :r"), {"r": run_id}).scalar_one()
    counts = {a: actions.count(a) for a in set(actions)}
    llm_audited = counts.get("llm.chat.completed", 0) + counts.get("llm.embed.completed", 0)
    assert llm_audited == llm_calls and llm_calls >= 2, f"{llm_calls} LLM calls but {llm_audited} audited"
    for required in (
        "memory.write.completed",
        "tool.call.completed",
        "approval.request",
        "approval.decide",
        "run.finish",
    ):
        assert counts.get(required), f"no audit row for {required}"
    policy = sum(v for k, v in counts.items() if k.startswith("policy."))
    assert policy >= 4, f"only {policy} policy decisions audited"
    chain = httpx.get(f"{hapmctl.AUDIT}/v1/audit/verify", timeout=60).json()
    assert chain["ok"], f"hash chain broken at seq {chain['first_bad_seq']}: {chain['reason']}"
    return f"{len(actions)} audit rows ({llm_calls} LLM, {policy} policy, memory, tool, approvals); chain verified over {chain['rows_checked']} rows"


def check_c(state: dict) -> str:  # type: ignore[type-arg]
    title = f"Rejected story {time.strftime('%H%M%S')}"
    sub = hapmctl.submit("requester", "The payments service release moved to Thursday after load tests passed.", title)
    approval = hapmctl.pending_approval(sub["run_id"])
    hapmctl.decide(approval["id"], "approver", "reject", "rejected by make verify").raise_for_status()
    final = hapmctl.wait_for(sub["run_id"], {"rejected"})
    assert title not in tracker_titles(), "a story was created for the rejected run"
    return f"run {sub['run_id']} is {final['state']} and created no story"


def check_d(state: dict) -> str:  # type: ignore[type-arg]
    evals = httpx.get(
        f"{hapmctl.EVAL}/v1/evals",
        params={"agent_id": "dummy_echo", "limit": 50},
        headers=hapmctl.auth("viewer"),
        timeout=10,
    ).json()["items"]
    agents = httpx.get(f"{hapmctl.ORCHESTRATOR}/v1/agents", headers=hapmctl.auth("viewer"), timeout=10).json()["items"]
    agent = next(a for a in agents if a["id"] == "dummy_echo")
    latest = next(e for e in evals if e["kind"] == "offline" and e["config_hash"] == agent["config_hash"])
    assert (
        latest["passed"] and latest["aggregate"] >= latest["threshold"]
    ), f"latest eval {latest['aggregate']} < {latest['threshold']}"
    assert agent["effective_autonomy"] == "act_with_approval", agent["reason"]
    return f"suite {latest['suite_id']} scored {latest['aggregate']} (threshold {latest['threshold']}); autonomy act_with_approval"


def check_e(state: dict) -> str:  # type: ignore[type-arg]
    resp = httpx.get(
        f"{hapmctl.ONEVIEW}/v1/runs/{state['run_id']}/timeline", headers=hapmctl.auth("viewer"), timeout=10
    )
    resp.raise_for_status()
    t = resp.json()
    s = t["summary"]
    for wanted in ("queued", "running", "waiting_approval", "succeeded"):
        assert wanted in s["states"], f"timeline is missing state {wanted}"
    kinds = {i["kind"] for i in t["items"]}
    missing = {"step", "llm", "tool", "memory", "policy", "approval", "state"} - kinds
    assert not missing, f"timeline is missing {sorted(missing)}"
    assert s["cost_usd"] > 0 and s["audit_rows"] > 0 and "run_id=" in s["audit_url"]
    return f"{len(t['items'])} timeline events; states {' -> '.join(dict.fromkeys(s['states']))}; cost ${s['cost_usd']}"


def check_f(state: dict) -> str:  # type: ignore[type-arg]
    title = f"Restart story {time.strftime('%H%M%S')}"
    sub = hapmctl.submit("requester", "Survive a worker restart during the LLM step.", title, fake_delay_seconds=8)
    hapmctl.wait_for(sub["run_id"], {"running"})
    time.sleep(2)
    compose("kill", "orchestrator-worker")
    time.sleep(2)
    compose("start", "orchestrator-worker")
    approval = hapmctl.pending_approval(sub["run_id"], timeout=120)
    hapmctl.decide(approval["id"], "approver", "approve", "after restart").raise_for_status()
    final = hapmctl.wait_for(sub["run_id"], {"succeeded"}, timeout=180)
    count = tracker_titles().count(title)
    assert count == 1, f"expected exactly one story, found {count}"
    return f"worker killed during the LLM step and restarted; run {final['state']} with exactly one story"


CHECKS: list[tuple[str, str, Callable[[dict], str]]] = [  # type: ignore[type-arg]
    ("a", "run state is succeeded", check_a),
    (
        "b",
        "audit rows for every LLM call, memory access, tool call, policy decision and approval; chain verifies",
        check_b,
    ),
    ("c", "the same task rejected creates no story", check_c),
    ("d", "the eval suite ran and meets its threshold", check_d),
    ("e", "the OneView-lite API shows the complete timeline", check_e),
    ("f", "killing the worker mid-run and restarting it still completes the run", check_f),
]


def main() -> int:
    if not STATE.exists():
        print("No demo state found. Run make demo first.")
        return 1
    state = json.loads(STATE.read_text(encoding="utf-8"))
    failures = 0
    for key, name, fn in CHECKS:
        try:
            detail = fn(state)
            print(f"PASS ({key}) {name}\n         {detail}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"FAIL ({key}) {name}\n         {exc}", flush=True)
            traceback.print_exc(limit=1)
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
