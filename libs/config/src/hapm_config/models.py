"""Typed models for each config file."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hapm_config.refs import is_ref


def _ref(value: str | None) -> str | None:
    if value is not None and not is_ref(value):
        raise ValueError("must be a reference such as env:NAME or kv:NAME, never a secret value")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoConfig(Strict):
    """One repository. Adding a repository means adding one file in config/repos/."""

    name: str = Field(pattern=r"^[a-z][a-z0-9\-]*$")
    backend: Literal["mock", "github"] = "mock"
    github_repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
    mock_path: str | None = None
    default_branch: str = "main"
    languages: list[str] = Field(default_factory=lambda: ["python"])
    build_command: str | None = None
    test_command: str = "python -m unittest discover -s tests -q"
    sandbox_image: str = "python:3.12.15-slim-trixie"
    excluded_paths: list[str] = Field(default_factory=lambda: [".git/*", "vendor/*", "node_modules/*", "*.min.js"])
    jira_project: str = Field(pattern=r"^[A-Z][A-Z0-9]+$")
    approvers: list[str] = Field(default_factory=list)
    max_new_tickets: int = Field(default=10, ge=1, le=100)
    scan_budget_usd: float = Field(default=1.0, gt=0)
    max_file_bytes: int = Field(default=200_000, gt=0)


class JiraConfig(Strict):
    backend: Literal["mock", "jira"] = "mock"
    site_url: str | None = None
    deployment: Literal["cloud", "data_center"] = "cloud"
    allowed_projects: list[str] = Field(default_factory=list)
    epics_in_scope: list[str] = Field(default_factory=list)
    issue_types: dict[str, str] = Field(default_factory=lambda: {"bug": "Bug", "enhancement": "Story"})
    email_ref: str | None = None
    token_ref: str | None = None

    _check_email = field_validator("email_ref")(_ref)
    _check_token = field_validator("token_ref")(_ref)


class GithubConfig(Strict):
    backend: Literal["mock", "github"] = "mock"
    api_url: str = "https://api.github.com"
    organisation: str | None = None
    app_id_ref: str | None = None
    installation_id_ref: str | None = None
    private_key_ref: str | None = None
    token_ref: str | None = None  # fine-grained PAT, an alternative to the GitHub App for pilots

    _check = field_validator("app_id_ref", "installation_id_ref", "private_key_ref", "token_ref")(_ref)


class GatePolicy(Strict):
    tickets: list[str] = Field(default_factory=lambda: ["approver", "admin"])
    plan: list[str] = Field(default_factory=lambda: ["approver", "admin"])
    pull_request: list[str] = Field(default_factory=lambda: ["approver", "admin"])


class ApproversConfig(Strict):
    default: GatePolicy = Field(default_factory=GatePolicy)
    repos: dict[str, GatePolicy] = Field(default_factory=dict)

    def for_repo(self, repo: str) -> GatePolicy:
        return self.repos.get(repo, self.default)


class SandboxConfig(Strict):
    allowed_registries: list[str] = Field(default_factory=list)
    cpus: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=512, ge=128, le=8192)
    pids_limit: int = Field(default=256, ge=32)
    timeout_seconds: int = Field(default=120, ge=10, le=3600)
    max_diff_lines: int = Field(default=300, ge=1)
    retry_limit: int = Field(default=3, ge=1, le=10)
    denied_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/*", "deploy/*", "*.pem", "*.key", "*secret*", "*credential*"]
    )

    @field_validator("allowed_registries")
    @classmethod
    def urls(cls, v: list[str]) -> list[str]:
        for url in v:
            if not re.match(r"^https://", url):
                raise ValueError("registries must be https URLs")
        return v


class ProjectConfig(Strict):
    repos: dict[str, RepoConfig]
    jira: JiraConfig
    github: GithubConfig
    approvers: ApproversConfig
    sandbox: SandboxConfig
    sources: dict[str, str] = Field(default_factory=dict)  # file kind -> path actually loaded

    def repo(self, name: str) -> RepoConfig:
        if name not in self.repos:
            raise KeyError(f"repository {name} is not onboarded")
        return self.repos[name]
