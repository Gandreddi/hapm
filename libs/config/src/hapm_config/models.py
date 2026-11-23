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


LlmMode = Literal["off", "local", "azure"]


class RepoEngines(Strict):
    graphify: bool = True
    scip: Literal["auto", "off"] = "auto"


class RepoSentinel(Strict):
    """Per-repository Sentinel settings (the `sentinel:` section of config/repos/<name>.yaml)."""

    enabled: bool = False
    tiers: list[int] = Field(default_factory=lambda: [1, 2, 3])
    node_budget: int = Field(default=1000, ge=10)
    enrich: bool = True
    schedule: str | None = None
    engines: RepoEngines = Field(default_factory=RepoEngines)
    llm: LlmMode | None = None  # overrides config/sentinel.yaml for this repository
    service_names: list[str] = Field(default_factory=list)  # host names other services use to call this one

    @field_validator("tiers")
    @classmethod
    def tiers_known(cls, v: list[int]) -> list[int]:
        if not v or any(t not in (1, 2, 3) for t in v):
            raise ValueError("tiers must be a non-empty subset of [1, 2, 3]")
        return sorted(set(v))


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
    sentinel: RepoSentinel = Field(default_factory=RepoSentinel)


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


class ScipIndexer(Strict):
    name: str
    version: str
    image: str = "hapm-sentinel-scip:0.5.2"
    # Shell command run in /work; {name} and {version} are the project's package name and version.
    command: str = ""
    memory_mb: int = Field(default=2048, ge=256, le=8192)


class GraphifyEngine(Strict):
    enabled: bool = True
    package: Literal["graphifyy"] = "graphifyy"
    version: str = "0.9.115"
    image: str = "hapm-sentinel-graphify:0.9.115"
    command: str = "graphify update . --force > /tmp/graphify.log 2>&1; test -f graphify-out/graph.json"
    output: str = "graphify-out/graph.json"
    timeout_seconds: int = Field(default=900, ge=10)
    memory_mb: int = Field(default=1024, ge=256, le=8192)


def _default_indexers() -> dict[str, "ScipIndexer"]:
    return {
        "python": ScipIndexer(
            name="scip-python",
            version="0.6.8",
            command=(
                "echo '[]' > /tmp/env.json && scip-python index --quiet --cwd /work --project-name {name} "
                "--project-version {version} --environment /tmp/env.json --output /tmp/index.scip "
                "&& scip print --json /tmp/index.scip > sentinel-scip.json"
            ),
        ),
        "typescript": ScipIndexer(
            name="scip-typescript",
            version="0.6.2",
            command=(
                "scip-typescript index --infer-tsconfig --output /tmp/index.scip "
                "&& scip print --json /tmp/index.scip > sentinel-scip.json"
            ),
        ),
    }


class ScipEngine(Strict):
    enabled: bool = True
    cli_version: str = "0.5.2"
    output: str = "sentinel-scip.json"
    timeout_seconds: int = Field(default=1800, ge=10)
    indexers: dict[str, ScipIndexer] = Field(default_factory=_default_indexers)  # language -> indexer


class SentinelEngines(Strict):
    graphify: GraphifyEngine = Field(default_factory=GraphifyEngine)
    scip: ScipEngine = Field(default_factory=ScipEngine)
    runner: Literal["recorded", "sandbox"] = "recorded"
    recorded_dir: str = "tests/fixtures/sentinel/recorded"


class SentinelBudgets(Strict):
    per_repo_usd: float = Field(default=1.0, gt=0)
    total_usd: float = Field(default=20.0, gt=0)
    per_repo_tokens: int = Field(default=200_000, gt=0)
    total_tokens: int = Field(default=5_000_000, gt=0)


class SentinelAliases(Strict):
    enrich: str = "enrich"
    ask: str = "ask"
    embed: str = "embed_local"


class SentinelEnrichment(Strict):
    label_retries: int = Field(default=3, ge=0, le=10)
    token_cap: int = Field(default=2000, ge=200)
    tiers: list[int] = Field(default_factory=lambda: [1, 2])
    prompt_version: str = "1"
    backoff_seconds: float = Field(default=1.0, ge=0)


class SentinelEmbeddings(Strict):
    enabled: bool = True
    dimension: int | None = Field(default=None, ge=8, le=8192)


class SentinelExport(Strict):
    html_node_limit: int = Field(default=7000, ge=10)


class SentinelConfig(Strict):
    """config/sentinel.yaml: fleet-wide Sentinel settings."""

    concurrency: int = Field(default=4, ge=1, le=32)
    budgets: SentinelBudgets = Field(default_factory=SentinelBudgets)
    llm: LlmMode = "off"
    aliases: SentinelAliases = Field(default_factory=SentinelAliases)
    enrichment: SentinelEnrichment = Field(default_factory=SentinelEnrichment)
    embeddings: SentinelEmbeddings = Field(default_factory=SentinelEmbeddings)
    export: SentinelExport = Field(default_factory=SentinelExport)
    refresh_schedule: str = "0 2 * * *"
    retention_shas: int = Field(default=3, ge=1)
    engines: SentinelEngines = Field(default_factory=SentinelEngines)
    relation_map: dict[str, str] = Field(
        default_factory=lambda: {
            "calls": "CALLS",
            "imports": "IMPORTS",
            "imports_from": "IMPORTS",
            "inherits": "INHERITS",
            "contains": "CONTAINS",
            "method": "CONTAINS",
            "uses": "DEPENDS_ON",
            "references": "REFERENCES",
        }
    )
    capabilities: list[str] = Field(default_factory=list)
    service_aliases: dict[str, str] = Field(default_factory=dict)  # host name -> repository name


class ProjectConfig(Strict):
    repos: dict[str, RepoConfig]
    jira: JiraConfig
    github: GithubConfig
    approvers: ApproversConfig
    sandbox: SandboxConfig
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    sources: dict[str, str] = Field(default_factory=dict)  # file kind -> path actually loaded

    def repo(self, name: str) -> RepoConfig:
        if name not in self.repos:
            raise KeyError(f"repository {name} is not onboarded")
        return self.repos[name]
