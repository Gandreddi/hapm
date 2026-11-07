import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from hapm_core.artifacts import MemoryArtifactStore, make_artifact_store
from hapm_core.audit import GENESIS_HASH, compute_row_hash, verify_rows
from hapm_core.hashing import content_hash


def chain(n: int) -> list[dict[str, Any]]:
    rows, prev = [], GENESIS_HASH
    start = datetime(2026, 11, 7, tzinfo=UTC)
    for i in range(n):
        row: dict[str, Any] = {
            "seq": i + 1,
            "id": uuid.uuid4(),
            "ts": start + timedelta(seconds=i),
            "run_id": "run_1",
            "task_id": "task_1",
            "actor": "agent:dummy_echo",
            "action": f"step.{i}",
            "resource": "r",
            "decision": "allow",
            "policy_rule_id": "rule",
            "inputs_hash": None,
            "outputs_hash": None,
            "trace_id": None,
            "prev_hash": prev,
        }
        row["row_hash"] = compute_row_hash(prev, row)
        prev = row["row_hash"]
        rows.append(row)
    return rows


def test_valid_chain_verifies() -> None:
    result = verify_rows(chain(5))
    assert result.ok and result.rows_checked == 5


def test_edited_row_is_detected() -> None:
    rows = chain(5)
    rows[2]["decision"] = "deny"
    result = verify_rows(rows)
    assert not result.ok and result.first_bad_seq == 3
    assert result.reason == "row content does not match row_hash"


def test_deleted_row_is_detected() -> None:
    rows = chain(5)
    del rows[1]
    result = verify_rows(rows)
    assert not result.ok and result.first_bad_seq == 3


def test_timestamp_representation_does_not_matter() -> None:
    rows = chain(1)
    rows[0]["ts"] = rows[0]["ts"].astimezone(UTC).isoformat()
    assert verify_rows(rows).ok


def test_artifact_store_is_content_addressed() -> None:
    store = MemoryArtifactStore()
    digest = store.put({"b": 1, "a": 2})
    assert digest == content_hash({"a": 2, "b": 1})
    assert store.get(digest) == b'{"a":2,"b":1}'
    assert store.get("missing") is None
    assert isinstance(make_artifact_store(None, "x"), MemoryArtifactStore)
