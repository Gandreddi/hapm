"""make pilot: run the Developer agent on the real repositories in config/repos (office laptop only).

Refuses to start until `hapm config check --pilot` reports every value live and present and the
office data approval is recorded. Approvals happen in the portal inbox (http://127.0.0.1:3000/inbox).
`make pilot-report` writes docs/pilot-report.md from what actually happened.
"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from hapm_config import load_project
from sqlalchemy import text

from scripts import hapmctl
from scripts.verify import ENGINE

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "pilot-report.md"


def ready() -> bool:
    code = subprocess.call(["uv", "run", "hapm", "config", "check", "--pilot"], cwd=ROOT)  # noqa: S603, S607 - fixed command
    return code == 0


def start(repo_names: list[str]) -> int:
    if not ready():
        print(
            "\nPilot refused: fill in the MISSING values above in config/*.yaml (see docs/runbook.md, 'Office setup')."
        )
        return 2
    for name in repo_names:
        sub = hapmctl.submit_task("requester", "developer", {"mode": "repo_scan", "repo": name})
        print(
            f"{name}: run {sub['run_id']} started (autonomy {sub['autonomy']}). Approve or reject each gate in the inbox:"
        )
        print(f"   http://127.0.0.1:3000/runs/{sub['run_id']}")
    return 0


def report() -> int:
    project = load_project()
    lines = [
        "# Developer agent pilot report",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from the HAPM database. Repositories marked mock use the "
        "fixture repository and mock Jira/GitHub, so their numbers prove the plumbing only.",
        "",
        "| Repository | Backend | Runs | Index files / chunks | LLM cost (USD) | Findings proposed | Accepted | Rejected | "
        "Plans approved | Sandbox runs (passed) | PRs opened | Avg run minutes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    totals_cost, totals_minutes, measured = 0.0, 0.0, 0
    with ENGINE.connect() as conn:
        for name, repo in project.repos.items():
            q = {"r": name}
            runs = conn.execute(
                text(
                    "SELECT r.id, extract(epoch FROM r.updated_at - r.created_at) AS secs FROM runs r "
                    "JOIN tasks t ON t.id = r.task_id WHERE r.agent_id = 'developer' AND r.mode = 'live' "
                    "AND t.input->>'repo' = :r"
                ),
                q,
            ).all()
            ids = [r.id for r in runs]
            idx = conn.execute(
                text("SELECT COALESCE(max(files),0), COALESCE(max(chunks),0) FROM code_index_state WHERE repo = :r"), q
            ).one()
            cost = float(
                conn.execute(
                    text("SELECT COALESCE(sum(cost_usd),0) FROM llm_usage WHERE run_id = ANY(:ids)"), {"ids": ids}
                ).scalar_one()
            )
            f = conn.execute(
                text(
                    "SELECT count(*) AS proposed, count(*) FILTER (WHERE status='ticketed') AS accepted, "
                    "count(*) FILTER (WHERE status='rejected') AS rejected FROM findings WHERE run_id = ANY(:ids)"
                ),
                {"ids": ids},
            ).one()
            plans = conn.execute(
                text(
                    "SELECT count(*) FROM approvals WHERE run_id = ANY(:ids) AND id LIKE '%approve_plan' "
                    "AND status='approved'"
                ),
                {"ids": ids},
            ).scalar_one()
            sbx = conn.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE exit_code = 0) FROM sandbox_runs WHERE run_id = ANY(:ids)"
                ),
                {"ids": ids},
            ).one()
            prs = conn.execute(
                text("SELECT count(*) FROM github_pull_requests WHERE idempotency_key = ANY(:keys)"),
                {"keys": [f"{i}:pr" for i in ids]},
            ).scalar_one()
            minutes = (sum(float(r.secs or 0) for r in runs) / len(runs) / 60) if runs else 0.0
            if runs:
                totals_cost += cost / len(runs)
                totals_minutes += minutes
                measured += 1
            lines.append(
                f"| {name} | {repo.backend} | {len(runs)} | {idx[0]} / {idx[1]} | {cost:.4f} | {f.proposed} | {f.accepted} | "
                f"{f.rejected} | {plans} | {sbx[0]} ({sbx[1]}) | {prs} | {minutes:.1f} |"
            )
    precision = "n/a"
    with ENGINE.connect() as conn:
        decided = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE status='ticketed'), count(*) FILTER (WHERE status IN "
                "('ticketed','rejected')) FROM findings"
            )
        ).one()
        if decided[1]:
            precision = f"{decided[0] / decided[1]:.2f} ({decided[0]} of {decided[1]} human-reviewed findings accepted)"
    lines += ["", f"**Measured precision (human acceptance):** {precision}", ""]
    if measured:
        lines += [
            "## Scaling estimate (estimate, not a measurement)",
            "",
            f"Average per repository run: ${totals_cost / measured:.4f} LLM cost and {totals_minutes / measured:.1f} minutes "
            f"of wall time including human approval waits. Projected for 150 repositories, one scan each: about "
            f"${150 * totals_cost / measured:.2f} and {150 * totals_minutes / measured / 60:.1f} hours of agent time, before "
            "human review. Real repositories are larger than the fixture; re-measure after the first real repository.",
        ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        sys.exit(report())
    sys.exit(start(sys.argv[1:] or list(load_project().repos)))
