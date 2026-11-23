"""M20 pilot: 1 repository, then 5, then every enabled repository, with a report per repository and fleet.

Refuses to start unless the configuration is complete for a real pilot: enabled repositories on the
GitHub backend, the sandbox engine runner, and (when llm is not off) data approval plus a passing model
health check. `--rehearsal` runs the same flow on the fixtures and labels every number as a rehearsal.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hapm_config import ProjectConfig, load_project

from hapm_sentinel.build import BuildContext, build_repo, enabled_repos, link_fleet
from hapm_sentinel.engines.runner import runner_for
from hapm_sentinel.llm import health_check, load_providers
from hapm_sentinel.ontology import ontology
from hapm_sentinel.sources import reader_for
from hapm_sentinel.validate import v1_schema, v2_integrity, v4_evidence

REPORT = Path(__file__).resolve().parents[4] / "docs" / "sentinel" / "pilot-report.md"


def readiness(project: ProjectConfig, rehearsal: bool) -> list[str]:
    missing: list[str] = []
    names = enabled_repos(project)
    if not names:
        missing.append("config/repos: no repository has sentinel.enabled: true")
    if not rehearsal:
        mock = [n for n in names if project.repos[n].backend == "mock"]
        if mock:
            missing.append(f"repositories on the mock backend (not a pilot): {', '.join(mock)}")
        if project.sentinel.engines.runner != "sandbox":
            missing.append("config/sentinel.yaml engines.runner must be sandbox (real graphify and SCIP)")
    if project.sentinel.llm != "off":
        report = health_check(project, load_providers())
        missing += [f"llm {name}: {detail}" for name, ok, detail in report.checks if not ok]
    return missing


def run_pilot(stage: str, rehearsal: bool = False, store: Any = None) -> int:
    project = load_project()
    missing = readiness(project, rehearsal)
    if missing:
        print("Sentinel pilot refused. Missing:")
        for m in missing:
            print(f"  - {m}")
        return 2
    if store is None:
        from hapm_core.db import make_engine

        from hapm_sentinel.cli import database_url
        from hapm_sentinel.store import SentinelStore

        store = SentinelStore(make_engine(database_url()))
    ctx = BuildContext(
        project,
        store,
        reader_for("local" if rehearsal else "connector"),
        runner_for(project.sentinel.engines),
        ontology(),
    )
    names = enabled_repos(project)
    count = {"1": 1, "5": 5}.get(stage, len(names))
    chosen = names[:count]
    started = time.monotonic()
    results = [build_repo(ctx, n, force=False, fleet_run_id=f"pilot-{stage}") for n in chosen]
    links = link_fleet(ctx)
    elapsed = time.monotonic() - started
    heads = store.heads()
    for r in results:  # an unchanged repository reports the numbers of the build that produced its head
        if r["status"] == "unchanged" and r["repo"] in heads:
            last = store.run(heads[r["repo"]]["run_id"]) or {}
            stats = last.get("stats") or {}
            r.update(
                {
                    "precision_tier": last.get("precision_tier"),
                    "nodes": stats.get("nodes"),
                    "edges": stats.get("edges"),
                    "duration_ms": stats.get("duration_ms", 0),
                    "engines": last.get("engines") or {},
                    "llm": last.get("llm") or {},
                }
            )
    nodes, edges = store.current_graph(chosen)
    checks = [
        v1_schema(ctx.ontology, nodes, edges),
        v2_integrity(nodes, edges),
        v4_evidence(edges, store.line_counts()),
    ]
    write_report(project, stage, rehearsal, results, links, checks, elapsed)
    print(f"pilot stage {stage}: {len(chosen)} repositories, report in {REPORT.as_posix()}")
    return 0 if all(r["status"] in ("succeeded", "unchanged") for r in results) else 1


def write_report(
    project: ProjectConfig,
    stage: str,
    rehearsal: bool,
    results: list[dict[str, Any]],
    links: dict[str, Any],
    checks: list[Any],
    elapsed: float,
) -> None:
    built = [r for r in results if r["status"] == "succeeded"]
    avg = sum(r.get("duration_ms", 0) for r in built) / max(len(built), 1) / 1000
    per_150 = avg * 150 / project.sentinel.concurrency
    lines = [
        f"# Sentinel pilot report (stage {stage})",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}."
        + (" **Rehearsal on fixtures: these numbers prove the flow, not production behaviour.**" if rehearsal else ""),
        "",
        f"LLM mode: `{project.sentinel.llm}`. Engine runner: `{project.sentinel.engines.runner}`. Wall time: {elapsed:.1f} s.",
        "",
        "| Repository | Status | Precision tier | Nodes | Edges | Build s | Engines | Enrichment |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        engines = ", ".join(f"{k}:{v.get('status')}" for k, v in (r.get("engines") or {}).items())
        llm = r.get("llm", {})
        enrich = (
            "off"
            if llm.get("mode", "off") == "off"
            else f"{llm.get('enriched', 0)} enriched, {llm.get('prompt_tokens', 0) + llm.get('completion_tokens', 0)} tokens"
        )
        lines.append(
            f"| {r['repo']} | {r['status']} | {r.get('precision_tier', '-')} | {r.get('nodes', '-')} | {r.get('edges', '-')} | {r.get('duration_ms', 0) / 1000:.1f} | {engines} | {enrich} |"
        )
    lines += [
        "",
        f"Cross-repository edges: {links.get('edges', 0)} {json.dumps(links.get('confidence', {}))}; relations {json.dumps(links.get('relations', {}))}.",
        "",
        "## Validators",
        "",
        *[f"- {c.line()}" for c in checks],
        "- Validators 3, 5, 6 and 7 need ground truth and golden questions for these repositories; run `sentinel verify` once they exist, and fill in docs/sentinel/review-sample.yaml for measured precision.",
        "",
        "## Developer agent effect",
        "",
        "Compare finding precision and location accuracy for scans with and without `context_pack` (the Developer agent records the pack in its intake output). Not measured automatically here.",
        "",
        "## Estimate for 150+ repositories (labelled estimate, not a measurement)",
        "",
        f"Average build {avg:.1f} s per repository at concurrency {project.sentinel.concurrency}: about {per_150 / 60:.1f} minutes for 150 repositories without enrichment. "
        "Enrichment adds roughly (higher-level nodes x ~900 tokens) per repository on the first build; unchanged code is served from the cache afterwards.",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
