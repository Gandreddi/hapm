"""Inputs for validators 6 and 7: the question planner under test and the vector-search baseline."""

from collections.abc import Callable
from typing import Any

import yaml
from hapm_codeintel.index import index_snapshot, search_code
from hapm_codeintel.snapshot import Snapshot
from hapm_config import ProjectConfig

from hapm_sentinel.ask import plan
from hapm_sentinel.llm import GatewayLLM
from hapm_sentinel.query import QueryContract
from hapm_sentinel.sources import fixture_head
from hapm_sentinel.validate import FIXTURES


def templated_golden() -> list[dict[str, Any]]:
    """30 extra contract-only questions derived from the seeded truth, so validator 6 has 50 questions."""
    truth = yaml.safe_load((FIXTURES / "truth.yaml").read_text(encoding="utf-8"))
    functions: list[str] = []
    modules: list[str] = []
    pairs: list[tuple[str, str]] = []
    for rels in truth["edges"].values():
        for a, b in rels.get("CALLS", []):
            fa, fb = a.split("#")[1], b.split("#")[1]
            functions += [fa, fb]
            pairs.append((fa, fb))
        for _, b in rels.get("IMPORTS", []):
            modules.append(b)
    out: list[dict[str, Any]] = []
    for name in sorted(set(functions))[:14]:
        out.append({"q": f"Explain `{name}`", "expect": {"intent": "explain", "entities": [name]}})
    for path in sorted(set(modules))[:8]:
        out.append({"q": f"What depends on `{path}`?", "expect": {"intent": "impact", "entities": [path]}})
    for a, b in pairs[:8]:
        out.append(
            {
                "q": f"Is there a path between `{a}` and `{b}`?",
                "expect": {"intent": "path", "entities": [a], "target": b},
            }
        )
    for name in sorted(set(functions)):  # top up with find questions so there are exactly 30
        if len(out) >= 30:
            break
        out.append({"q": f"Find `{name}`", "expect": {"intent": "find", "entities": [name]}})
    return out


def golden_planner(project: ProjectConfig) -> Callable[[str], tuple[QueryContract | None, str]]:
    llm = GatewayLLM(run_id="sentinel-verify-planner")

    def planner(question: str) -> tuple[QueryContract | None, str]:
        contract, _status, meta = plan(llm, project.sentinel.aliases.ask, question)
        return contract, str(meta.get("provider", ""))

    return planner


def vector_baseline(
    project: ProjectConfig, store: Any, files: dict[str, dict[str, str]], k: int = 4
) -> Callable[[str], str] | None:
    """The existing pgvector code index (Phase 1 search_code) as a baseline, embedded through the gateway."""
    llm = GatewayLLM(run_id="sentinel-verify-vector")
    shas: dict[str, str] = {}
    try:
        for repo, contents in files.items():
            cfg = project.repo(repo)
            snap = Snapshot.build(
                repo, "verify-" + fixture_head(contents)[:16], contents, cfg.excluded_paths, cfg.max_file_bytes
            )
            index_snapshot(store.engine, snap, lambda texts: llm.embed("embed", texts))
            shas[repo] = snap.sha
    except Exception:
        return None

    def search(question: str) -> str:
        vector = llm.embed("embed", [question])[0]
        hits: list[str] = []
        for repo, sha in shas.items():
            for row in search_code(store.engine, repo, sha, vector, k):
                hits.append(f"{repo}:{row['path']}:{row['start_line']}\n{row['content']}")
        return "\n".join(hits)[:8000]

    return search
