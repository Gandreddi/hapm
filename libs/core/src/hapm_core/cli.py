"""HAPM command line: hapm audit verify, hapm eval run."""

import json
import sys

import httpx
import typer

app = typer.Typer(help="HAPM command line", no_args_is_help=True)
audit_app = typer.Typer(help="Audit log commands", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation commands", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
app.add_typer(eval_app, name="eval")
config_app = typer.Typer(help="Project configuration commands", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("check")
def config_check(
    pilot: bool = typer.Option(False, help="Exit 2 unless everything needed for the pilot is live and present"),
) -> None:
    """Validate config/*.yaml, resolve every reference without printing secrets, show live vs mocked."""
    from pathlib import Path

    import yaml
    from hapm_config import check, load_project
    from hapm_config.loader import config_dir

    directory = config_dir()
    project = load_project(directory)
    providers_path = next(
        (p for p in (directory / "providers.yaml", directory / "providers.example.yaml") if p.exists()), None
    )
    providers = yaml.safe_load(Path(providers_path).read_text(encoding="utf-8")) if providers_path else None
    report = check(project, providers)
    typer.echo(f"config directory: {directory}")
    for kind, source in {**report.sources, "providers": providers_path.name if providers_path else "missing"}.items():
        typer.echo(f"  {kind}: {source}")
    for title, items in (("LIVE", report.live), ("MOCKED", report.mocked), ("MISSING", report.missing)):
        typer.echo(f"{title} ({len(items)})")
        for item in items:
            typer.echo(f"  - {item}")
    typer.echo(f"pilot ready: {report.pilot_ready}")
    if pilot and not report.pilot_ready:
        sys.exit(2)


@audit_app.command("verify")
def audit_verify(
    url: str = typer.Option("http://127.0.0.1:8003", envvar="HAPM_AUDIT_URL"),
) -> None:
    """Verify the audit hash chain; exits 1 if it is broken."""
    result = httpx.get(f"{url}/v1/audit/verify", timeout=60).raise_for_status().json()
    typer.echo(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


@eval_app.command("run")
def eval_run(
    agent_id: str = typer.Argument(...),
    suite_id: str = typer.Argument(...),
    version: str = typer.Option("1"),
    url: str = typer.Option("http://127.0.0.1:8004", envvar="HAPM_EVAL_URL"),
    token: str = typer.Option(..., envvar="HAPM_TOKEN", help="Bearer token with the admin role"),
) -> None:
    """Run an eval suite; exits 1 if the aggregate is below the threshold."""
    body = {"agent_id": agent_id, "suite_id": suite_id, "version": version}
    result = (
        httpx.post(f"{url}/v1/evals/run", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=600)
        .raise_for_status()
        .json()
    )
    typer.echo(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
