"""Project configuration: repositories, Jira, GitHub, approvers, sandbox, Sentinel and LLM providers.

Every office-specific value lives in config/*.yaml. Secrets are references (env:NAME or kv:NAME),
never values. When a real file is missing, the committed *.example.yaml is used, which points
every feature at mocks so local development and tests always run.
"""

from hapm_config.loader import CheckReport, check, load_project
from hapm_config.models import (
    ApproversConfig,
    GithubConfig,
    JiraConfig,
    LlmMode,
    ProjectConfig,
    RepoConfig,
    RepoSentinel,
    SandboxConfig,
    SentinelConfig,
)
from hapm_config.refs import RefError, resolve_ref

__all__ = [
    "ApproversConfig",
    "CheckReport",
    "GithubConfig",
    "JiraConfig",
    "LlmMode",
    "ProjectConfig",
    "RefError",
    "RepoConfig",
    "RepoSentinel",
    "SandboxConfig",
    "SentinelConfig",
    "check",
    "load_project",
    "resolve_ref",
]
