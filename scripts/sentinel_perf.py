"""Sentinel performance on a synthetic 50-repository fleet (M19 targets).

Targets: full build within budget; incremental refresh of one changed file under 60 s; sentinel_query p95
under 2 s. The synthetic repositories have no engine recordings, so they exercise the ast fallback, the
linkers, storage and queries; real graphify and SCIP timings come from `sentinel record` and the pilot.
Synthetic rows use the `perf-` prefix and are removed afterwards; the fixture fleet is relinked.
"""

import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from hapm_config import RepoConfig, load_project
from hapm_config.models import RepoSentinel
from hapm_core.db import make_engine
from hapm_sentinel.build import BuildContext, build_repo, link_fleet
from hapm_sentinel.cli import database_url
from hapm_sentinel.engines.runner import RecordedRunner
from hapm_sentinel.ontology import ontology
from hapm_sentinel.query import Executor, QueryContract
from hapm_sentinel.sources import LocalReader
from hapm_sentinel.store import SentinelStore
from sqlalchemy import text

REPOS = 50
MODULES = 12
FUNCTIONS = 6
REPORT = Path(__file__).resolve().parents[1] / "docs" / "sentinel" / "perf-report.json"


def make_repo(root: Path, i: int) -> None:
    base = root / f"perf_{i:02d}"
    pkg = base / f"svc{i:02d}"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    deps = '"perf-lib>=1.0"' if i else ""
    (base / "pyproject.toml").write_text(
        f'[project]\nname = "perf-{i:02d}"\nversion = "1.0.0"\ndependencies = [{deps}]\n', encoding="utf-8"
    )
    nxt = (i + 1) % REPOS
    (pkg / "config.py").write_text(f'NEXT_URL = "http://perf-{nxt:02d}:8000"\n', encoding="utf-8")
    (pkg / "api.py").write_text(
        "from fastapi import APIRouter\n\nimport httpx\n\n"
        f"from svc{i:02d}.config import NEXT_URL\nfrom svc{i:02d}.m00 import f0\n\nrouter = APIRouter()\n\n\n"
        f'@router.get("/items/{{item_id}}")\ndef get_item(item_id: str) -> int:\n    return f0()\n\n\n'
        'def call_next(item_id: str) -> int:\n    return httpx.get(f"{NEXT_URL}/items/{item_id}").status_code\n',
        encoding="utf-8",
    )
    for m in range(MODULES):
        lines = []
        if m + 1 < MODULES:
            lines.append(f"from svc{i:02d}.m{m + 1:02d} import f0 as next_f0\n")
        for f in range(FUNCTIONS):
            body = f"f{f + 1}()" if f + 1 < FUNCTIONS else ("next_f0()" if m + 1 < MODULES else "0")
            lines.append(f"\n\ndef f{f}() -> int:\n    return {body}\n")
        (pkg / f"m{m:02d}.py").write_text("".join(lines), encoding="utf-8")


def main() -> int:
    project = load_project()
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-perf-"))
    store = SentinelStore(make_engine(database_url()))
    try:
        for i in range(REPOS):
            make_repo(tmp, i)
        lib = tmp / "perf_lib"
        (lib / "perflib").mkdir(parents=True)
        (lib / "pyproject.toml").write_text('[project]\nname = "perf-lib"\nversion = "1.2.0"\n', encoding="utf-8")
        (lib / "perflib" / "__init__.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
        repos = {
            f"perf-{i:02d}": RepoConfig(
                name=f"perf-{i:02d}",
                mock_path=f"perf_{i:02d}",
                jira_project="PERF",
                sentinel=RepoSentinel(enabled=True),
            )
            for i in range(REPOS)
        }
        repos["perf-lib"] = RepoConfig(
            name="perf-lib", mock_path="perf_lib", jira_project="PERF", sentinel=RepoSentinel(enabled=True)
        )
        perf_project = project.model_copy(update={"repos": repos})
        ctx = BuildContext(perf_project, store, LocalReader(tmp), RecordedRunner(tmp / "no-recordings"), ontology())

        started = time.monotonic()
        results = [build_repo(ctx, name, force=True, fleet_run_id="perf") for name in repos]
        links = link_fleet(ctx)
        full = time.monotonic() - started
        nodes = sum(r.get("nodes", 0) for r in results)
        failed = [r for r in results if r["status"] != "succeeded"]

        target = tmp / "perf_07" / "svc07" / "m03.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n\ndef added() -> int:\n    return f0()\n", encoding="utf-8"
        )
        started = time.monotonic()
        refreshed = build_repo(ctx, "perf-07", fleet_run_id="perf")
        link_fleet(ctx)
        incremental = time.monotonic() - started

        executor = Executor(store)
        rng = random.Random(1)  # noqa: S311 - reproducible query mix, not security
        latencies = []
        for _ in range(100):
            i = rng.randrange(REPOS)
            contract = rng.choice(
                [
                    QueryContract(intent="impact", entities=[f"perf-{i:02d}:svc{i:02d}/m05.py#f0"], depth=3),
                    QueryContract(intent="explain", entities=[f"perf-{i:02d}:svc{i:02d}/api.py#get_item"]),
                    QueryContract(
                        intent="path",
                        entities=[f"perf-{i:02d}:svc{i:02d}/api.py#call_next"],
                        target=f"perf-{(i + 1) % REPOS:02d}:api#GET /items/{{item_id}}",
                        depth=2,
                    ),
                    QueryContract(intent="find", entities=["get_item"], repos=[f"perf-{i:02d}"]),
                ]
            )
            t = time.perf_counter()
            result = executor.run(contract)
            latencies.append((time.perf_counter() - t) * 1000)
            assert result["status"] in ("answered", "not_found"), result
        p95 = statistics.quantiles(latencies, n=20)[18]
        report = {
            "repositories": len(repos),
            "nodes": nodes,
            "cross_repo_edges": links["edges"],
            "failed": len(failed),
            "full_build_seconds": round(full, 1),
            "incremental_refresh_seconds": round(incremental, 2),
            "incremental_status": refreshed["status"],
            "query_p50_ms": round(statistics.median(latencies), 1),
            "query_p95_ms": round(p95, 1),
            "targets": {"incremental_refresh_seconds": 60, "query_p95_ms": 2000},
            "engines": "ast fallback (synthetic repositories have no engine recordings)",
        }
        report["passed"] = not failed and incremental < 60 and p95 < 2000
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(json.dumps(report, indent=1))
        return 0 if report["passed"] else 1
    finally:
        with store.engine.begin() as conn:
            for table in (
                "sentinel_nodes",
                "sentinel_edges",
                "sentinel_files",
                "sentinel_facts",
                "sentinel_heads",
                "sentinel_runs",
            ):
                conn.execute(text(f"DELETE FROM {table} WHERE repo LIKE 'perf-%'"))  # noqa: S608 - fixed table names
        real = BuildContext(
            project, store, LocalReader(), RecordedRunner(Path(project.sentinel.engines.recorded_dir)), ontology()
        )
        link_fleet(real)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
