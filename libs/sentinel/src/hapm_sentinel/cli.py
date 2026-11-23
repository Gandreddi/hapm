"""`sentinel` command line: build, link, query, ask, export, verify, diff, doctor, record, pilot."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from hapm_config import load_project
from hapm_config.loader import config_dir

app = typer.Typer(help="Sentinel: read-only knowledge graph of every onboarded repository", no_args_is_help=True)
ROOT = Path(__file__).resolve().parents[4]
REPORTS = ROOT / "docs" / "sentinel"


def database_url() -> str:
    """HAPM_DATABASE_URL, else the local stack (127.0.0.1, not localhost: avoids a slow IPv6 lookup on Windows)."""
    if os.environ.get("HAPM_DATABASE_URL"):
        return os.environ["HAPM_DATABASE_URL"]
    password = "hapm-app-local"  # noqa: S105 - local dev default, same as CoreSettings
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("HAPM_APP_DB_PASSWORD="):
                password = line.split("=", 1)[1].strip()
    return f"postgresql+psycopg://hapm_app:{password}@127.0.0.1:5432/hapm"


def _store() -> Any:
    from hapm_core.db import make_engine
    from hapm_sentinel.store import SentinelStore

    return SentinelStore(make_engine(database_url()))


def _context(runner: str | None, source: str) -> Any:
    from hapm_sentinel.build import BuildContext
    from hapm_sentinel.engines.runner import runner_for
    from hapm_sentinel.ontology import ontology
    from hapm_sentinel.sources import reader_for

    project = load_project()
    return BuildContext(project, _store(), reader_for(source), runner_for(project.sentinel.engines, runner), ontology())


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command()
def build(
    repo: list[str] = typer.Option(
        None, "--repo", help="Repository name; repeat for several. Default: every enabled one."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignore every cache and rebuild from scratch (after ontology, engine, model or embedding changes).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be rebuilt and the estimated LLM token use; run nothing."
    ),
    runner: str = typer.Option(None, help="recorded or sandbox (default: config engines.runner)."),
    source: str = typer.Option(
        "local", help="local (fixture folders) or connector (GitHub MCP connector, read tools only)."
    ),
    temporal: bool = typer.Option(
        False, "--temporal", help="Run as the sentinel_fleet Temporal workflow instead of in this process."
    ),
) -> None:
    """Build the graph incrementally (unchanged repositories are skipped), then link the fleet."""
    from hapm_sentinel.build import build_fleet, plan_fleet

    ctx = _context(runner, source)
    if dry_run:
        _print(plan_fleet(ctx, repo or None, force))
        return
    if temporal:
        from hapm_sentinel.workflows import start_fleet

        _print(start_fleet(repo or None, force, runner, source))
        return
    result = build_fleet(ctx, repo or None, force=force)
    for r in result["repos"]:
        typer.echo(
            f"{r['repo']}: {r['status']} {r.get('precision_tier', '')} nodes={r.get('nodes', '-')} edges={r.get('edges', '-')} {r.get('error', '')}"
        )
    typer.echo(
        f"links: {json.dumps(result['links'].get('relations', {}))} confidence={json.dumps(result['links'].get('confidence', {}))}"
    )
    if any(r["status"] == "failed" for r in result["repos"]):
        sys.exit(1)


@app.command()
def link() -> None:
    """Rebuild only the cross-repository layer from the current heads."""
    from hapm_sentinel.build import link_fleet

    _print(link_fleet(_context(None, "local")))


@app.command()
def query(
    contract: str = typer.Argument(..., help='JSON contract, e.g. {"intent": "impact", "entities": ["to_cents"]}'),
) -> None:
    """Run one JSON query contract deterministically (no model involved)."""
    from hapm_sentinel.query import Executor, QueryContract

    _print(Executor(_store()).run(QueryContract.model_validate_json(contract)))


@app.command()
def ask(question: str) -> None:
    """Plain-language question -> contract (gateway `ask` alias) -> answer. Disabled when llm is off."""
    from hapm_sentinel.ask import ask as run_ask

    _print(run_ask(load_project(), _store(), question))


@app.command()
def export(
    fmt: str = typer.Option("json", "--format", help="json, graphml or html"),
    repo: list[str] = typer.Option(None, "--repo"),
    node_limit: int = typer.Option(None, "--node-limit", help="HTML only; default export.html_node_limit"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Export the current graph. JSON and GraphML are complete; HTML is capped and says how much it left out."""
    from hapm_sentinel import export as ex

    nodes, edges = _store().current_graph(repo or None)
    node_list = list(nodes.values())
    if fmt == "json":
        text = ex.to_json(node_list, edges)
    elif fmt == "graphml":
        text = ex.to_graphml(node_list, edges)
    elif fmt == "html":
        text = ex.to_html(node_list, edges, node_limit or load_project().sentinel.export.html_node_limit)
    else:
        raise typer.BadParameter("format must be json, graphml or html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out} ({len(node_list)} nodes, {len(edges)} edges)")


@app.command()
def stats() -> None:
    """Per-repository node and edge counts, confidence mix and cross-repository edges."""
    _print(_store().stats())


@app.command()
def diff(repo: str, from_sha: str = typer.Option(..., "--from"), to_sha: str = typer.Option(..., "--to")) -> None:
    """Graph regression between two SHAs of a repository (node and edge counts by type)."""
    from collections import Counter

    store = _store()
    a_nodes, b_nodes = store.repo_nodes(repo, from_sha), store.repo_nodes(repo, to_sha)
    a_edges, b_edges = store.repo_edges(repo, from_sha), store.repo_edges(repo, to_sha)
    _print(
        {
            "nodes_added": sorted(set(b_nodes) - set(a_nodes))[:50],
            "nodes_removed": sorted(set(a_nodes) - set(b_nodes))[:50],
            "node_types": {
                "from": Counter(n.type for n in a_nodes.values()),
                "to": Counter(n.type for n in b_nodes.values()),
            },
            "relations": {"from": Counter(e.relation for e in a_edges), "to": Counter(e.relation for e in b_edges)},
            "cross_repo": {"from": sum(e.cross_repo for e in a_edges), "to": sum(e.cross_repo for e in b_edges)},
        }
    )


@app.command()
def doctor() -> None:
    """Model health check: reachable, model id listed exactly, JSON output, embedding dimension. No secrets printed."""
    from hapm_sentinel.llm import health_check

    report = health_check(load_project())
    typer.echo(f"llm mode: {report.mode}")
    for name, ok, detail in report.checks:
        typer.echo(f"  {'OK  ' if ok else 'FAIL'} {name}: {detail}")
    sys.exit(0 if report.ok else 2)


@app.command()
def record(repo: list[str] = typer.Option(..., "--repo")) -> None:
    """Run the real engines in the sandbox on fixture repositories and save their outputs for offline tests."""
    from hapm_sentinel.record import record as run_record

    for meta in run_record(load_project(), repo):
        typer.echo(f"{meta['repo']} {meta['sha'][:12]} {json.dumps(meta['engines'])}")


@app.command()
def discover(org: str = typer.Option(..., "--org")) -> None:
    """List an organisation's repositories and write draft config files (never enabled automatically)."""
    from hapm_sentinel.discover import write_drafts

    for name, action in write_drafts(load_project(), org, config_dir()):
        typer.echo(f"{name}: {action}")


@app.command()
def verify(
    review: Path = typer.Option(None, help="A reviewed sample (verdicts filled in) to measure INFERRED precision"),
    skip_vector: bool = typer.Option(False, help="Skip the vector baseline (needs the gateway)"),
) -> None:
    """M19 validators 1-7 over the current graph; exits 1 if any falls below its threshold."""
    from hapm_sentinel import validate as v
    from hapm_sentinel.ontology import ontology
    from hapm_sentinel.planner_eval import golden_planner, templated_golden, vector_baseline
    from hapm_sentinel.sources import LocalReader

    project = load_project()
    store = _store()
    nodes, edges = store.current_graph()
    counts = store.line_counts()
    golden = v.load_golden()
    reader = LocalReader()
    files = {n: reader.read(r).snapshot.files for n, r in project.repos.items() if r.sentinel.enabled and r.mock_path}
    checks = [
        v.v1_schema(ontology(), nodes, edges),
        v.v2_integrity(nodes, edges),
        v.v3_structural(nodes, edges, [r for r in store.runs(limit=200) if r["status"] == "succeeded"]),
        v.v4_evidence(edges, counts),
        v.v5_confidence(edges, REPORTS / "review-sample.yaml", review),
        v.v6_contract(golden + templated_golden(), golden_planner(project)),
        v.v7_answers(store, golden, files, None if skip_vector else vector_baseline(project, store, files)),
    ]
    for c in checks:
        typer.echo(c.line())
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "verify-report.json").write_text(
        json.dumps([c.__dict__ for c in checks], indent=1, default=str), encoding="utf-8"
    )
    store.cache_put(
        "verify",
        "latest",
        [{"number": c.number, "name": c.name, "passed": c.passed, "value": c.value, "label": c.label} for c in checks],
    )
    typer.echo(
        f"{sum(c.passed for c in checks)}/{len(checks)} validators passed; details in docs/sentinel/verify-report.json"
    )
    sys.exit(0 if all(c.passed for c in checks) else 1)


@app.command()
def pilot(
    stage: str = typer.Option("1", help="1, 5 or all: how many enabled repositories to build"),
    rehearsal: bool = typer.Option(
        False, "--rehearsal", help="Run the pilot flow on the fixtures; the report says so."
    ),
) -> None:
    """M20 pilot: refuses to run unless config is complete; builds 1, then 5, then all, and writes a report."""
    from hapm_sentinel.pilot import run_pilot

    code = run_pilot(stage, rehearsal)
    sys.exit(code)


if __name__ == "__main__":
    app()
