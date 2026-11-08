"""Jira MCP server. Every tool is limited to the projects allowed in config/jira.yaml."""

from pathlib import Path
from typing import Any

from hapm_config import load_project
from hapm_core.db import make_engine
from hapm_core.settings import REPO_ROOT, CoreSettings
from hapm_mcp_client.server import build_server
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from hapm_jira.backends import JiraBackend, JiraError, MockJira, RestJira


class JiraSettings(CoreSettings):
    service_name: str = "jira-connector"
    mcp_allowed_hosts: list[str] = ["jira-connector:8000", "127.0.0.1:8103", "localhost:8103"]
    jira_seed_path: Path = REPO_ROOT / "tests" / "fixtures" / "jira" / "seed.yaml"


def build(backend: JiraBackend, allowed_projects: list[str], hosts: list[str], ready: Any) -> FastMCP:
    mcp = build_server("hapm-jira", hosts, ready)

    def project_of(key: str) -> str:
        project = key.split("-", 1)[0]
        if project not in allowed_projects:
            raise JiraError(f"project {project} is not in jira.allowed_projects")
        return project

    @mcp.tool()
    def jira_get_epic(epic_key: str) -> dict[str, Any]:
        """Read an epic."""
        project_of(epic_key)
        return backend.get_issue(epic_key)

    @mcp.tool()
    def jira_list_epic_issues(epic_key: str) -> dict[str, Any]:
        """List the child issues of an epic."""
        project_of(epic_key)
        return {"epic": epic_key, "issues": backend.list_epic_issues(epic_key)}

    @mcp.tool()
    def jira_search_issues(
        project: str, query: str = "", labels: list[str] | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Search issues in an allowed project by summary text and labels."""
        project_of(f"{project}-0")
        return {"issues": backend.search(project, query or None, labels or [], max(1, min(limit, 100)))}

    @mcp.tool()
    def jira_get_issue(issue_key: str) -> dict[str, Any]:
        """Read one issue."""
        project_of(issue_key)
        return backend.get_issue(issue_key)

    @mcp.tool()
    def jira_create_issue(
        project: str,
        issue_type: str,
        summary: str,
        description: str,
        idempotency_key: str,
        labels: list[str] | None = None,
        epic_key: str = "",
    ) -> dict[str, Any]:
        """Create an issue. Repeating a call with the same idempotency_key returns the same issue."""
        project_of(f"{project}-0")
        if not summary.strip():
            raise JiraError("summary is required")
        return backend.create_issue(
            project, issue_type, summary, description, labels or [], epic_key or None, idempotency_key
        )

    @mcp.tool()
    def jira_add_comment(issue_key: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Add a comment to an issue."""
        project_of(issue_key)
        return backend.add_comment(issue_key, body, idempotency_key)

    return mcp


def main() -> None:
    from hapm_telemetry import configure_logging

    settings = JiraSettings()
    configure_logging(settings.service_name, settings.log_level)
    project = load_project()
    engine = make_engine(settings.database_url)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    backend: JiraBackend = (
        MockJira(engine, settings.jira_seed_path) if project.jira.backend == "mock" else RestJira(project.jira)
    )
    allowed = project.jira.allowed_projects or sorted({r.jira_project for r in project.repos.values()})
    build(backend, allowed, settings.mcp_allowed_hosts, ready).run(transport="streamable-http")


if __name__ == "__main__":
    main()
