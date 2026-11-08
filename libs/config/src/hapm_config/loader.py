"""Load config/*.yaml (falling back to *.example.yaml) and report what is live or mocked."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hapm_config.models import ApproversConfig, GithubConfig, JiraConfig, ProjectConfig, RepoConfig, SandboxConfig
from hapm_config.refs import RefError, resolve_ref

DEFAULT_DIR = Path(__file__).resolve().parents[4] / "config"


def _read(directory: Path, stem: str, sources: dict[str, str]) -> dict[str, Any]:
    for name in (f"{stem}.yaml", f"{stem}.example.yaml"):
        path = directory / name
        if path.exists():
            sources[stem] = str(path.name)
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources[stem] = "defaults"
    return {}


def config_dir() -> Path:
    return Path(os.environ.get("HAPM_CONFIG_DIR", str(DEFAULT_DIR)))


def load_project(directory: Path | None = None) -> ProjectConfig:
    directory = directory or config_dir()
    sources: dict[str, str] = {}
    repos: dict[str, RepoConfig] = {}
    repo_dir = directory / "repos"
    real = (
        sorted(p for p in repo_dir.glob("*.yaml") if not p.name.endswith(".example.yaml")) if repo_dir.exists() else []
    )
    files = real or (sorted(repo_dir.glob("*.example.yaml")) if repo_dir.exists() else [])
    for path in files:
        repo = RepoConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if repo.name in repos:
            raise ValueError(f"repository {repo.name} is defined twice")
        repos[repo.name] = repo
    sources["repos"] = ", ".join(p.name for p in files) or "none"
    return ProjectConfig(
        repos=repos,
        jira=JiraConfig.model_validate(_read(directory, "jira", sources)),
        github=GithubConfig.model_validate(_read(directory, "github", sources)),
        approvers=ApproversConfig.model_validate(_read(directory, "approvers", sources)),
        sandbox=SandboxConfig.model_validate(_read(directory, "sandbox", sources)),
        sources=sources,
    )


@dataclass
class CheckReport:
    live: list[str] = field(default_factory=list)
    mocked: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def pilot_ready(self) -> bool:
        return not self.missing and not self.mocked


def _need(report: CheckReport, label: str, ref: str | None) -> None:
    try:
        resolve_ref(ref)
    except RefError as exc:
        report.missing.append(f"{label}: {exc}")


def check(project: ProjectConfig, providers: dict[str, Any] | None = None) -> CheckReport:
    """Resolve every reference without revealing values; list what is live, mocked or missing."""
    report = CheckReport(sources=dict(project.sources))
    if project.jira.backend == "mock":
        report.mocked.append("jira: mock backend")
    else:
        report.live.append(f"jira: {project.jira.site_url}")
        if not project.jira.site_url:
            report.missing.append("jira.site_url")
        if not project.jira.allowed_projects:
            report.missing.append("jira.allowed_projects")
        _need(report, "jira.token_ref", project.jira.token_ref)
        if project.jira.deployment == "cloud":
            _need(report, "jira.email_ref", project.jira.email_ref)
    if project.github.backend == "mock":
        report.mocked.append("github: mock backend")
    else:
        report.live.append(f"github: {project.github.api_url}")
        if project.github.token_ref:
            _need(report, "github.token_ref", project.github.token_ref)
        else:
            for label in ("app_id_ref", "installation_id_ref", "private_key_ref"):
                _need(report, f"github.{label}", getattr(project.github, label))
    if not project.repos:
        report.missing.append("config/repos: no repositories onboarded")
    for repo in project.repos.values():
        if repo.backend == "mock":
            report.mocked.append(f"repo {repo.name}: mock backend")
        else:
            report.live.append(f"repo {repo.name}: {repo.github_repo}")
            if not repo.github_repo:
                report.missing.append(f"repo {repo.name}: github_repo")
        if project.jira.backend == "jira" and repo.jira_project not in project.jira.allowed_projects:
            report.missing.append(f"repo {repo.name}: Jira project {repo.jira_project} not in jira.allowed_projects")
    if providers is not None:
        live = [n for n, p in providers.get("providers", {}).items() if p.get("type") != "fake"]
        usable = []
        for name in live:
            cfg = providers["providers"][name]
            refs = [cfg.get(k) for k in ("token_ref", "api_key_ref", "endpoint_ref") if cfg.get(k)]
            try:
                for r in refs:
                    resolve_ref(r)
                usable.append(name)
            except RefError:
                pass
        if usable:
            report.live.append(f"llm providers: {', '.join(usable)}")
        else:
            report.mocked.append("llm: only the Fake provider is usable")
        if not providers.get("data_approval"):
            report.missing.append(
                "providers.data_approval: confirm office data rules allow sending code to the provider"
            )
    return report
