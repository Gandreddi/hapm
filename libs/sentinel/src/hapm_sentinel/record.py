"""`sentinel record`: run the real engines in the sandbox on fixture repositories and save their outputs.

The recordings let CI verify mapping, merging and precedence without graphify or SCIP installed. Each
folder holds meta.json with the fixture SHA and engine versions, so a stale recording is detected.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hapm_config import ProjectConfig
from hapm_sentinel.build import _project_identity, code_languages
from hapm_sentinel.engines.runner import REPO_ROOT, SandboxRunner, recorded_folder
from hapm_sentinel.facts import repo_facts
from hapm_sentinel.sources import LocalReader


def record(project: ProjectConfig, repos: list[str], out_dir: Path | None = None) -> list[dict[str, Any]]:
    base = out_dir or REPO_ROOT / project.sentinel.engines.recorded_dir
    runner = SandboxRunner(project.sentinel.engines)
    reader = LocalReader()
    results = []
    for name in repos:
        repo = project.repo(name)
        snap = reader.read(repo).snapshot
        folder = recorded_folder(base, repo)
        folder.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "repo": name,
            "sha": snap.sha,
            "recorded_at": datetime.now(UTC).isoformat(),
            "engines": {},
        }
        g = runner.graphify(repo, snap)
        meta["engines"]["graphify"] = {
            "status": g.status,
            "version": project.sentinel.engines.graphify.version,
            "error": g.error,
        }
        if g.status == "ok" and g.data is not None:
            (folder / "graphify.json").write_text(json.dumps(g.data, indent=1, sort_keys=True), encoding="utf-8")
        identity = _project_identity(repo_facts(snap.files), repo)
        for language in code_languages(snap):
            indexer = project.sentinel.engines.scip.indexers.get(language)
            if indexer is None:
                continue
            s = runner.scip(repo, snap, language, identity)
            meta["engines"][f"scip-{language}"] = {
                "status": s.status,
                "indexer": f"{indexer.name}@{indexer.version}",
                "error": s.error,
            }
            if s.status == "ok" and s.data is not None:
                (folder / f"scip-{language}.json").write_text(
                    json.dumps(s.data, indent=1, sort_keys=True), encoding="utf-8"
                )
        (folder / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
        results.append(meta)
    return results
