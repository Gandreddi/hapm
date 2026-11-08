"""GitHub MCP server. Only onboarded repositories; writes only to new hapm/* branches; no merge tool exists."""

import re
from typing import Any

from hapm_config import ProjectConfig, load_project
from hapm_core.db import make_engine
from hapm_core.settings import REPO_ROOT, CoreSettings
from hapm_mcp_client.server import build_server
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from hapm_github.backends import GithubBackend, GithubError, MockGithub, RestGithub

BRANCH = re.compile(r"^hapm/[A-Za-z0-9._\-/]{1,120}$")
MAX_FILES = 50


class GithubSettings(CoreSettings):
    service_name: str = "github-connector"
    mcp_allowed_hosts: list[str] = ["github-connector:8000", "127.0.0.1:8104", "localhost:8104"]


def build(project: ProjectConfig, backends: dict[str, GithubBackend], hosts: list[str], ready: Any) -> FastMCP:
    mcp = build_server("hapm-github", hosts, ready)

    def repo_and_backend(name: str) -> tuple[Any, GithubBackend]:
        repo = project.repo(name)  # KeyError for anything not onboarded
        return repo, backends[repo.backend]

    @mcp.tool()
    def github_head(repo: str) -> dict[str, Any]:
        """Return the commit SHA at the head of the default branch."""
        r, b = repo_and_backend(repo)
        return {"repo": repo, "branch": r.default_branch, "sha": b.head(r)}

    @mcp.tool()
    def github_list_tree(repo: str, ref: str = "") -> dict[str, Any]:
        """List every file at a commit (default: head of the default branch)."""
        r, b = repo_and_backend(repo)
        sha = ref or b.head(r)
        return {"repo": repo, "sha": sha, "files": b.list_tree(r, sha)}

    @mcp.tool()
    def github_read_file(repo: str, path: str, ref: str = "") -> dict[str, Any]:
        """Read one file at a commit."""
        r, b = repo_and_backend(repo)
        return {"repo": repo, "path": path, "ref": ref, "content": b.read_file(r, path, ref)}

    @mcp.tool()
    def github_list_commits(repo: str, limit: int = 10) -> dict[str, Any]:
        """Recent commits on the default branch."""
        r, b = repo_and_backend(repo)
        return {"commits": b.list_commits(r, max(1, min(limit, 50)))}

    @mcp.tool()
    def github_list_issues_and_prs(repo: str) -> dict[str, Any]:
        """Open pull requests."""
        r, b = repo_and_backend(repo)
        return {"pull_requests": b.list_pull_requests(r)}

    @mcp.tool()
    def github_push_branch(
        repo: str, branch: str, base_sha: str, files: dict[str, str], message: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Create a new hapm/* branch from base_sha with the given file contents. Never touches the default branch."""
        r, b = repo_and_backend(repo)
        if not BRANCH.match(branch) or branch == r.default_branch:
            raise GithubError("branch must be a new hapm/* branch, never the default branch")
        if not files or len(files) > MAX_FILES:
            raise GithubError(f"push 1-{MAX_FILES} files")
        return b.push_branch(r, branch, base_sha, files, message, idempotency_key)

    @mcp.tool()
    def github_open_pull_request(repo: str, head: str, title: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Open a pull request from a hapm/* branch into the default branch. A human reviews and merges it."""
        r, b = repo_and_backend(repo)
        if not BRANCH.match(head):
            raise GithubError("pull requests may only come from hapm/* branches")
        return b.open_pull_request(r, head, r.default_branch, title, body, idempotency_key)

    return mcp


def main() -> None:
    from hapm_telemetry import configure_logging

    settings = GithubSettings()
    configure_logging(settings.service_name, settings.log_level)
    project = load_project()
    engine = make_engine(settings.database_url)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    backends: dict[str, GithubBackend] = {"mock": MockGithub(engine, REPO_ROOT)}
    if project.github.backend == "github" or any(r.backend == "github" for r in project.repos.values()):
        backends["github"] = RestGithub(project.github)
    build(project, backends, settings.mcp_allowed_hosts, ready).run(transport="streamable-http")


if __name__ == "__main__":
    main()
