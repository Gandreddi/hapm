"""Append-only audit log with a hash chain.

Each row's hash covers its own fields plus the previous row's hash, so editing, deleting or
reordering any row breaks verification from that point on. Writes are serialised with a
transaction-level advisory lock so the chain never forks.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from sqlalchemy import Connection, text

from hapm_core.artifacts import ArtifactStore
from hapm_core.hashing import canonical_json, sha256_hex

GENESIS_HASH = "0" * 64
AUDIT_LOCK_KEY = 7_310_001
CHAINED_FIELDS = (
    "id",
    "ts",
    "run_id",
    "task_id",
    "actor",
    "action",
    "resource",
    "decision",
    "policy_rule_id",
    "inputs_hash",
    "outputs_hash",
    "trace_id",
)


@dataclass(frozen=True)
class AuditEntry:
    actor: str
    action: str
    resource: str
    decision: str
    run_id: str | None = None
    task_id: str | None = None
    policy_rule_id: str | None = None
    inputs: Any = None
    outputs: Any = None


@dataclass
class ChainResult:
    ok: bool
    rows_checked: int
    first_bad_seq: int | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _normalise_ts(ts: datetime | str) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


def compute_row_hash(prev_hash: str, row: dict[str, Any]) -> str:
    fields = {name: row.get(name) for name in CHAINED_FIELDS}
    fields["id"] = str(fields["id"])
    fields["ts"] = _normalise_ts(row["ts"])
    return sha256_hex(prev_hash + canonical_json(fields))


def _current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def record(conn: Connection, entry: AuditEntry, artifacts: ArtifactStore) -> dict[str, Any]:
    """Append one row using the caller's transaction and return it."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK_KEY})
    prev = conn.execute(text("SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1")).scalar_one_or_none()
    row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC),
        "run_id": entry.run_id,
        "task_id": entry.task_id,
        "actor": entry.actor,
        "action": entry.action,
        "resource": entry.resource,
        "decision": entry.decision,
        "policy_rule_id": entry.policy_rule_id,
        "inputs_hash": artifacts.put(entry.inputs) if entry.inputs is not None else None,
        "outputs_hash": artifacts.put(entry.outputs) if entry.outputs is not None else None,
        "trace_id": _current_trace_id(),
        "prev_hash": prev or GENESIS_HASH,
    }
    row["row_hash"] = compute_row_hash(row["prev_hash"], row)
    seq: int = conn.execute(
        text(
            "INSERT INTO audit_log (id, ts, run_id, task_id, actor, action, resource, decision, "
            "policy_rule_id, inputs_hash, outputs_hash, trace_id, prev_hash, row_hash) VALUES "
            "(:id, :ts, :run_id, :task_id, :actor, :action, :resource, :decision, "
            ":policy_rule_id, :inputs_hash, :outputs_hash, :trace_id, :prev_hash, :row_hash) "
            "RETURNING seq"
        ),
        row,
    ).scalar_one()
    row["seq"] = seq
    return row


def verify_rows(rows: Iterable[dict[str, Any]]) -> ChainResult:
    """Check rows in seq order: each must link to the previous hash and hash correctly."""
    prev = GENESIS_HASH
    count = 0
    for row in rows:
        count += 1
        if row["prev_hash"] != prev:
            return ChainResult(False, count, row["seq"], "prev_hash does not link to previous row")
        expected = compute_row_hash(prev, row)
        if row["row_hash"] != expected:
            return ChainResult(False, count, row["seq"], "row content does not match row_hash")
        prev = row["row_hash"]
    return ChainResult(True, count)


def verify_db(conn: Connection) -> ChainResult:
    result = conn.execute(text("SELECT * FROM audit_log ORDER BY seq"))
    return verify_rows(dict(r._mapping) for r in result)
