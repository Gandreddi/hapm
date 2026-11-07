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
