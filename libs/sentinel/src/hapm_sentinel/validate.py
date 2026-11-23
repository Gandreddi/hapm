"""M19 validators (make sentinel-verify). Each returns a Check with the measured value and its threshold.

1 schema, 2 integrity, 3 structural truth, 4 evidence, 5 confidence mix and review sample,
6 question -> contract accuracy, 7 answer quality against grep and vector baselines.
Fake-provider results prove plumbing only and are labelled so; they never count as a quality result.
"""

import json
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hapm_sentinel.ids import module_id, symbol_id
from hapm_sentinel.memstore import MemoryGraphStore
from hapm_sentinel.model import Edge, Node
from hapm_sentinel.ontology import Ontology
from hapm_sentinel.query import Executor, QueryContract

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "sentinel"


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    value: str
    detail: dict[str, Any] = field(default_factory=dict)
    label: str = ""  # e.g. "plumbing only (Fake provider)"

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{status} ({self.number}) {self.name}: {self.value}" + (f" [{self.label}]" if self.label else "")


def v1_schema(onto: Ontology, nodes: dict[str, Node], edges: list[Edge]) -> Check:
    types = {n.id: n.type for n in nodes.values()}
    node_problems = Counter(p for n in nodes.values() if (p := onto.node_problem(n)))
    edge_problems = Counter(p for e in edges if (p := onto.edge_problem(e, types)) and p != "dangling edge")
    bad = sum(node_problems.values()) + sum(edge_problems.values())
    total = len(nodes) + len(edges)
    return Check(
        1,
        "schema: nodes and edges conform to the ontology",
        bad == 0,
        f"{total - bad}/{total} conform",
        {"nodes": dict(node_problems), "edges": dict(edge_problems)},
    )


def v2_integrity(nodes: dict[str, Node], edges: list[Edge]) -> Check:
    dangling = [e for e in edges if e.source not in nodes or e.target not in nodes]
    linked = {e.source for e in edges} | {e.target for e in edges}
    candidates = [n for n in nodes.values() if n.type not in ("Repository", "Ticket")]
    orphans = [n.id for n in candidates if n.id not in linked]
    pct = 100 * len(orphans) / max(len(candidates), 1)
    keys = Counter((e.source, e.target, e.relation, e.repo, e.layer) for e in edges)
    dupes = sum(c - 1 for c in keys.values() if c > 1)
    ok = not dangling and pct < 5 and dupes == 0
    return Check(
        2,
        "integrity: no dangling edges or duplicates, orphans under 5%",
        ok,
        f"dangling {len(dangling)}, duplicates {dupes}, orphans {pct:.1f}%",
        {"dangling": [f"{e.source}->{e.target}" for e in dangling[:10]], "orphans": orphans[:20]},
    )


def _prf(predicted: set[Any], truth: set[Any]) -> tuple[float, float, list[Any], list[Any]]:
    tp = predicted & truth
    precision = len(tp) / len(predicted) if predicted else 1.0
    recall = len(tp) / len(truth) if truth else 1.0
    return precision, recall, sorted(predicted - truth), sorted(truth - predicted)


def v3_structural(
    nodes: dict[str, Node], edges: list[Edge], runs: list[dict[str, Any]], truth_path: Path | None = None
) -> Check:
    truth = yaml.safe_load((truth_path or FIXTURES / "truth.yaml").read_text(encoding="utf-8"))
    predicted: set[tuple[str, ...]] = set()
    expected: set[tuple[str, ...]] = set()
    for repo, rels in truth["edges"].items():
        for a, b in rels.get("IMPORTS", []):
            expected.add((module_id(repo, a), "IMPORTS", module_id(repo, b)))
        for a, b in rels.get("CALLS", []):
            pa, qa = a.split("#")
            pb, qb = b.split("#")
            expected.add((symbol_id(repo, pa, qa), "CALLS", symbol_id(repo, pb, qb)))
    fixture_repos = set(truth["edges"])
    for e in edges:
        if e.layer != "repo" or e.repo not in fixture_repos:
            continue
        target = nodes.get(e.target)
        if e.relation == "IMPORTS" and not e.target.startswith("lib:"):
            predicted.add((e.source, "IMPORTS", e.target))
        elif e.relation == "CALLS" and target is not None and target.type in ("Function", "Test"):
            predicted.add((e.source, "CALLS", e.target))
    precision, recall, extra, missed = _prf(predicted, expected)
    link_truth = {tuple(t) for t in truth["links"]}
    # Service-level CONSUMES edges repeat the function-level ones; truth lists the function-level edge.
    link_pred = {
        (e.source, e.relation, e.target) for e in edges if e.layer == "link" and not e.source.startswith("svc:")
    }
    lp, lr, lextra, lmissed = _prf(link_pred, link_truth)
    agree = sum(r.get("stats", {}).get("disagreements", {}).get("agree", 0) for r in runs)
    only = sum(r.get("stats", {}).get("disagreements", {}).get("graphify_only", 0) for r in runs)
    agreement = agree / (agree + only) if agree + only else 1.0
    ok = precision >= 0.95 and recall >= 0.90 and lp >= 0.95 and lr >= 0.90
    return Check(
        3,
        "structural truth: precision >= 0.95, recall >= 0.90 (fixtures)",
        ok,
        f"edges P={precision:.2f} R={recall:.2f}; links P={lp:.2f} R={lr:.2f}; graphify/SCIP agreement {agreement:.2f}",
        {
            "extra": [list(x) for x in extra],
            "missed": [list(x) for x in missed],
            "links_extra": [list(x) for x in lextra],
            "links_missed": [list(x) for x in lmissed],
            "scip_agreement": agreement,
        },
    )


def v4_evidence(edges: list[Edge], line_counts: dict[tuple[str, str, str], int]) -> Check:
    bad = []
    with_file = [e for e in edges if e.file]
    for e in with_file:
        count = line_counts.get((e.repo, e.sha, e.file or ""))
        last = e.line_end or e.line_start or 1
        if count is None or last > count or (e.line_start or 1) < 1:
            bad.append(f"{e.repo}:{e.file}:{e.line_start}")
    return Check(
        4,
        "evidence: every cited file and line exists at its SHA",
        not bad,
        f"{len(with_file) - len(bad)}/{len(with_file)} verified",
        {"bad": bad[:20]},
    )


def v5_confidence(edges: list[Edge], sample_path: Path, review_path: Path | None = None, seed: int = 7) -> Check:
    mix: dict[str, Counter[str]] = defaultdict(Counter)
    for e in edges:
        mix[e.repo][e.confidence] += 1
    pool = [e for e in edges if e.confidence == "INFERRED" or e.cross_repo]
    rng = random.Random(seed)  # noqa: S311 - reproducible review sample, not security
    sample = rng.sample(pool, max(min(len(pool), 5), len(pool) // 10)) if pool else []
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(
        yaml.safe_dump(
            {
                "instructions": "Set verdict to correct or wrong for each edge after checking the evidence; validator 5 then reports measured precision.",
                "edges": [
                    {
                        "source": e.source,
                        "relation": e.relation,
                        "target": e.target,
                        "confidence": e.confidence,
                        "repo": e.repo,
                        "file": e.file,
                        "line": e.line_start,
                        "verdict": None,
                    }
                    for e in sample
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    measured: float | None = None
    if review_path and review_path.exists():
        reviewed = yaml.safe_load(review_path.read_text(encoding="utf-8")).get("edges", [])
        verdicts = [r["verdict"] for r in reviewed if r.get("verdict") in ("correct", "wrong")]
        if verdicts:
            measured = verdicts.count("correct") / len(verdicts)
    share = {r: {k: f"{100 * v / sum(c.values()):.0f}%" for k, v in c.items()} for r, c in mix.items()}
    passed = measured is None or measured >= 0.85
    value = f"sample of {len(sample)} written to {sample_path.as_posix()}; " + (
        f"measured precision {measured:.2f}" if measured is not None else "awaiting human review"
    )
    return Check(
        5,
        "confidence mix and human review sample (precision >= 0.85)",
        passed,
        value,
        {"mix": share},
        "" if measured is not None else "not yet reviewed",
    )


def v6_contract(golden: list[dict[str, Any]], planner: Any) -> Check:
    """planner(question) -> (contract | None, provider). Exact match on intent and entities."""
    hits, providers, misses = 0, Counter[str](), []
    for item in golden:
        contract, provider = planner(item["q"])
        providers[provider] += 1
        expected = item["expect"]
        if (
            contract is not None
            and contract.intent == expected["intent"]
            and [e.lower() for e in contract.entities] == [e.lower() for e in expected["entities"]]
        ):
            hits += 1
        else:
            misses.append(
                {"q": item["q"], "got": contract.model_dump(include={"intent", "entities"}) if contract else None}
            )
    rate = hits / len(golden) if golden else 0.0
    fake = set(providers) == {"fake"}  # planner errors (provider "") never count as plumbing passes
    return Check(
        6,
        "question -> contract: intent and entity exact match >= 0.90",
        rate >= 0.90 or fake,
        f"{hits}/{len(golden)} = {rate:.2f}",
        {"providers": dict(providers), "misses": misses[:10]},
        "plumbing only (Fake provider)" if fake else "",
    )


def _coverage(text: str, facts: list[str]) -> float:
    low = text.lower()
    return sum(1 for f in facts if f.lower() in low) / len(facts) if facts else 1.0


def grep_baseline(files: dict[str, dict[str, str]], entity: str, budget_chars: int) -> str:
    """What an agent gets from grep: every line mentioning the entity, with path and line number."""
    name = entity.split(".")[-1].split(":")[-1]
    out = []
    for repo, contents in files.items():
        for path, text in contents.items():
            for i, line in enumerate(text.splitlines(), start=1):
                if re.search(rf"\b{re.escape(name)}\b", line):
                    out.append(f"{repo}:{path}:{i}: {line.strip()}")
    return "\n".join(out)[:budget_chars]


def v7_answers(store: Any, golden: list[dict[str, Any]], files: dict[str, dict[str, str]], vector: Any = None) -> Check:
    executor = Executor(store)
    rows = []
    for item in golden:
        contract = QueryContract(question=item["q"], **item["expect"])
        started = time.perf_counter()
        result = executor.run(contract)
        latency = (time.perf_counter() - started) * 1000
        text = json.dumps({k: result.get(k) for k in ("answer", "nodes", "edges", "evidence")}, default=str)
        grep_text = grep_baseline(files, item["expect"]["entities"][0], 8000)
        row = {
            "q": item["q"],
            "status": result["status"],
            "sentinel": _coverage(text, item["facts"]),
            "grep": _coverage(grep_text, item["facts"]),
            "sentinel_tokens": len(text) // 4,
            "grep_tokens": len(grep_text) // 4,
            "latency_ms": round(latency, 1),
        }
        if vector is not None:
            vtext = vector(item["q"])
            row["vector"] = _coverage(vtext, item["facts"])
            row["vector_tokens"] = len(vtext) // 4
        rows.append(row)
    n = max(len(rows), 1)
    mean = {
        k: sum(r[k] for r in rows) / n for k in ("sentinel", "grep", "sentinel_tokens", "grep_tokens", "latency_ms")
    }
    if vector is not None:
        mean["vector"] = sum(r["vector"] for r in rows) / n
    beats = mean["sentinel"] > mean["grep"] and mean["sentinel"] > mean.get("vector", -1)
    value = (
        f"key-fact coverage sentinel {mean['sentinel']:.2f} vs grep {mean['grep']:.2f}"
        + (f" vs vector {mean['vector']:.2f}" if "vector" in mean else "")
        + f"; tokens {mean['sentinel_tokens']:.0f} vs {mean['grep_tokens']:.0f}; latency {mean['latency_ms']:.0f} ms"
    )
    return Check(
        7,
        "answer quality: Sentinel beats grep and vector baselines on golden questions",
        beats,
        value,
        {"mean": mean, "rows": rows},
    )


def load_golden(path: Path | None = None) -> list[dict[str, Any]]:
    return list(yaml.safe_load((path or FIXTURES / "golden.yaml").read_text(encoding="utf-8"))["questions"])


def memory_store(
    nodes: dict[str, Node], edges: list[Edge], line_counts: dict[tuple[str, str, str], int]
) -> MemoryGraphStore:
    return MemoryGraphStore(nodes.values(), edges, line_counts)
