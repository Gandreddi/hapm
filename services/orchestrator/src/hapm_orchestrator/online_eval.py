"""Online evaluation: score real runs with the same scorers, using the suite's online checks."""

import json
from collections.abc import Callable
from typing import Any

import yaml
from hapm_core.governance import Governor
from hapm_core.ids import new_id
from hapm_core.scoring import score_output
from hapm_orchestrator.agents import AgentRegistry, config_hash
from sqlalchemy import text


def make_online_scorer(governor: Governor, agents: AgentRegistry) -> Callable[[str, dict[str, Any]], None]:
    def score(run_id: str, output: dict[str, Any]) -> None:
        with governor.engine.connect() as conn:
            run = conn.execute(text("SELECT agent_id, mode FROM runs WHERE id = :r"), {"r": run_id}).one()
        if run.mode == "eval":
            return
        agent = agents.load(run.agent_id)
        if not agent.eval_suite:
            return
        path = agents.directory / "evals" / f"{agent.eval_suite}.yaml"
        if not path.exists():
            return
        expect = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("online", {})
        if not expect:
            return
        result = score_output(expect, output)
        eval_id = new_id("eval")
        passed = result["score"] >= agent.eval_threshold
        with governor.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO eval_results (id, kind, agent_id, suite_id, version, config_hash, run_id, aggregate, "
                    "threshold, passed, cases) VALUES (:i, 'online', :a, :s, :v, :h, :r, :agg, :t, :p, CAST(:c AS jsonb))"
                ),
                {
                    "i": eval_id,
                    "a": agent.id,
                    "s": agent.eval_suite,
                    "v": agent.version,
                    "h": config_hash(agent),
                    "r": run_id,
                    "agg": result["score"],
                    "t": agent.eval_threshold,
                    "p": passed,
                    "c": json.dumps([{"id": run_id, **result}]),
                },
            )
            governor.emit(
                conn,
                "eval.completed",
                {
                    "eval_id": eval_id,
                    "agent_id": agent.id,
                    "suite_id": agent.eval_suite,
                    "aggregate": result["score"],
                    "passed": passed,
                    "kind": "online",
                    "run_id": run_id,
                },
                subject=run_id,
            )

    return score
