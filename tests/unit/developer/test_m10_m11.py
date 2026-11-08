"""M10 and M11 unit checks: analysers, findings, autofixes, guardrails, sandbox payloads, metrics."""

import io
import struct
import tarfile
from pathlib import Path

import pytest
from hapm_codeintel import Snapshot, analyze_snapshot, apply_fix, dedupe, fingerprint, looks_secret, verify_location
from hapm_codeintel.fixes import FixError
from hapm_codeintel.index import chunk_file
from hapm_config import SandboxConfig
from hapm_core.scoring import findings_metrics, score_output
from hapm_developer.guardrails import check_change
from hapm_sandbox.docker_api import Limits, container_spec, demux, make_tar

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/repos/payments_service"


@pytest.fixture(scope="module")
def snap() -> Snapshot:
    files = {
        p.relative_to(FIXTURE).as_posix(): p.read_text(encoding="utf-8") for p in FIXTURE.rglob("*") if p.is_file()
    }
    return Snapshot.build("payments-service", "sha1", files, ["secrets/*"], 200_000)


def test_snapshot_drops_excluded_and_secret_files(snap: Snapshot) -> None:
    assert snap.skipped == {"secrets/config_local.py": "excluded"}
    built = Snapshot.build("r", "s", {"a.py": "key = 'sk_live_" + "A" * 24 + "'", "img.png": "x"}, [], 1000)
    assert built.skipped == {"a.py": "secret pattern", "img.png": "not text"}
    assert looks_secret("AKIA" + "A" * 16)


def test_analysers_find_all_rule_defects_with_exact_locations(snap: Snapshot) -> None:
    found = analyze_snapshot(snap)
    assert len(found) == 23
    assert all(verify_location(f, snap.files) for f in found)
    metrics = findings_metrics("tests/fixtures/repos/payments_service.truth.yaml", [f.model_dump() for f in found])
    assert metrics == {"detected": 23, "seeded": 25, "recall": 0.92, "precision": 1.0, "location_accuracy": 1.0}


def test_every_autofix_parses_and_resolves_its_finding(snap: Snapshot) -> None:
    fixable = [f for f in analyze_snapshot(snap) if f.rule in ("R1", "R2", "R3", "R8")]
    assert len(fixable) == 13  # R1 x3, R2 x4, R3 x3, R8 x3
    for finding in fixable:
        fixed = apply_fix(snap.files[finding.path], finding)
        again = Snapshot.build("payments-service", "sha2", {**snap.files, finding.path: fixed}, ["secrets/*"], 200_000)
        remaining = [f for f in analyze_snapshot(again) if f.fingerprint == finding.fingerprint]
        assert not remaining, f"{finding.rule} at {finding.path}:{finding.start_line} not resolved"
    todo = next(f for f in analyze_snapshot(snap) if f.rule == "R4")
    with pytest.raises(FixError):
        apply_fix(snap.files[todo.path], todo)


def test_unverifiable_locations_are_rejected_and_dedupe_caps(snap: Snapshot) -> None:
    findings = analyze_snapshot(snap)
    bad = findings[0].model_copy(update={"start_line": 999, "end_line": 1000})
    assert not verify_location(bad, snap.files)
    assert not verify_location(findings[0].model_copy(update={"path": "missing.py"}), snap.files)
    chosen, dropped = dedupe(findings + findings[:2], {findings[3].fingerprint}, limit=5)
    assert len(chosen) == 5 and dropped == 3
    assert [f.severity for f in chosen][:1] == ["high"]
    assert fingerprint("r", "p", "R2", None, "x == None") == fingerprint("r", "p", "R2", None, "x  ==  None")


def test_guardrails_block_and_flag() -> None:
    cfg = SandboxConfig(max_diff_lines=3)
    before = {
        "src/a.py": "x = 1\n",
        "tests/test_a.py": "assert a\nassert b\n",
        ".github/workflows/ci.yml": "on: push\n",
    }
    ok = check_change(before, {"src/a.py": "x = 2\n"}, cfg)
    assert ok.ok and ok.diff_lines == 2 and "+x = 2" in ok.diff
    denied = check_change(before, {".github/workflows/ci.yml": "on: pull_request\n"}, cfg)
    assert any("denied" in b for b in denied.blocked)
    secret = check_change(before, {"src/a.py": "x = 'ghp_" + "a" * 36 + "'\n"}, cfg)
    assert any("secret" in b for b in secret.blocked)
    big = check_change(before, {"src/a.py": "a\nb\nc\nd\n"}, cfg)
    assert any("limit" in b for b in big.blocked)
    weakened = check_change(before, {"tests/test_a.py": "assert a\n"}, SandboxConfig())
    assert weakened.ok and weakened.flags == ["removes assertions from tests/test_a.py"]
    assert check_change(before, {"tests/test_a.py": None}, SandboxConfig()).flags == [
        "deletes test file tests/test_a.py"
    ]


def test_sandbox_container_is_locked_down() -> None:
    spec = container_spec("python:3.12.15-slim-trixie", "python -m unittest", Limits(1.0, 512, 256, 60))
    host = spec["HostConfig"]
    assert spec["NetworkDisabled"] and host["NetworkMode"] == "none" and host["CapDrop"] == ["ALL"]
    assert spec["User"] == "65534:65534" and "no-new-privileges" in host["SecurityOpt"]
    assert host["Memory"] == 512 * 1024 * 1024 and host["PidsLimit"] == 256
    assert not any(e.split("=")[0] in ("GITHUB_TOKEN", "JIRA_API_TOKEN", "GITHUB_MODELS_TOKEN") for e in spec["Env"])


def test_sandbox_tar_and_log_demux() -> None:
    data = make_tar({"src/a.py": "x = 1\n", "tests/t.py": "pass\n"})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
        assert {"work", "work/src", "work/src/a.py", "work/tests/t.py"} <= set(names)
        assert all(m.uid == 65534 for m in tar.getmembers())
    frame = lambda stream, payload: struct.pack(">BxxxI", stream, len(payload)) + payload  # noqa: E731
    assert demux(frame(1, b"ok\n") + frame(2, b"err\n")) == "ok\nerr\n"


def test_index_chunks_cover_every_line() -> None:
    content = "\n".join(f"line {i}" for i in range(95))
    chunks = chunk_file("a.py", content)
    assert [(s, e) for s, e, _ in chunks] == [(1, 40), (41, 80), (81, 95)]


def test_developer_scorers() -> None:
    output = {
        "tool_calls": [{"tool": "jira_create_issue"}, {"tool": "github_open_pull_request"}],
        "outputs": {
            "implement": {"tests_passed": True},
            "intake": {"skipped": {"secrets/x.py": "excluded"}},
            "plan_work": {"kind": "fix", "finding": {"path": "src/a.py"}},
        },
    }
    result = score_output(
        {
            "tool_calls_include": ["jira_create_issue"],
            "tool_calls_exclude": ["github_push_branch"],
            "tests_passed": True,
            "skipped_paths": ["secrets/x.py"],
            "linked_path": "src/a.py",
            "plan_kind": "fix",
        },
        output,
    )
    assert result["score"] == 1.0, result
