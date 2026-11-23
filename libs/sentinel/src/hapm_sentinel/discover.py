"""`hapm repos discover --org <org>`: list an organisation's repositories and write DRAFT config files.

Drafts are written as config/repos/<name>.yaml with `sentinel.enabled: false` for a human to review.
Existing files are never overwritten and nothing is ever enabled automatically.
"""

import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from hapm_config import ProjectConfig, resolve_ref


def list_org_repos(project: ProjectConfig, org: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    gh = project.github
    if gh.backend == "mock":
        root = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "repos"
        return [
            {
                "name": p.name.replace("_", "-"),
                "full_name": f"{org}/{p.name}",
                "default_branch": "main",
                "language": None,
                "mock_path": f"tests/fixtures/repos/{p.name}",
            }
            for p in sorted(root.iterdir())
            if p.is_dir()
        ]
    token = resolve_ref(gh.token_ref, required=True) if gh.token_ref else None
    if not token:
        raise RuntimeError("repos discover needs github.token_ref (a read-only token) in config/github.yaml")
    http = client or httpx.Client(timeout=30)
    out: list[dict[str, Any]] = []
    url: str | None = f"{gh.api_url.rstrip('/')}/orgs/{org}/repos?per_page=100&type=all"
    while url:
        resp = http.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        for r in resp.json():
            if not r.get("archived"):
                out.append(
                    {
                        "name": r["name"].lower(),
                        "full_name": r["full_name"],
                        "default_branch": r.get("default_branch", "main"),
                        "language": r.get("language"),
                    }
                )
        match = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("link", ""))
        url = match.group(1) if match else None
    return out


def write_drafts(project: ProjectConfig, org: str, directory: Path) -> list[tuple[str, str]]:
    """Return (name, action) for each repository: 'draft written' or 'exists, left unchanged'."""
    results = []
    known = set(project.repos)
    target = directory / "repos"
    target.mkdir(parents=True, exist_ok=True)
    for repo in list_org_repos(project, org):
        name = re.sub(r"[^a-z0-9\-]", "-", repo["name"]).strip("-")
        path = target / f"{name}.yaml"
        if name in known or path.exists() or (target / f"{name}.example.yaml").exists():
            results.append((name, "exists, left unchanged"))
            continue
        language = (repo.get("language") or "python").lower()
        draft = {
            "name": name,
            "backend": "mock" if repo.get("mock_path") else "github",
            "github_repo": None if repo.get("mock_path") else repo["full_name"],
            "mock_path": repo.get("mock_path"),
            "default_branch": repo["default_branch"],
            "languages": [language],
            "jira_project": "TODO",
            "sentinel": {"enabled": False, "node_budget": 1000, "engines": {"graphify": True, "scip": "auto"}},
        }
        header = (
            "# DRAFT written by `hapm repos discover`. Review it, set jira_project, then set sentinel.enabled: true.\n"
        )
        path.write_text(header + yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")
        results.append((name, "draft written"))
    return results
