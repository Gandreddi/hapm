"""M1 DONE checks: tamper detection, append-only audit, outbox survives relay restart."""

import asyncio
import json
import time

import httpx
import pytest
from aiokafka import AIOKafkaConsumer
from hapm_core.audit import verify_db
from hapm_core.governance import Governor
from hapm_events import already_processed, emit
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from tests.integration.conftest import KAFKA, compose


def test_policy_decision_is_audited_and_chain_verifies(governor: Governor, app_engine: Engine) -> None:
    decision = governor.decide(
        "agent:itest", "tool.call", "create_story", attributes={"kind": "write"}, run_id="run_itest"
    )
    assert decision.effect.value == "require_approval"
    with app_engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM audit_log WHERE run_id = 'run_itest' ORDER BY seq DESC LIMIT 1")).one()
        assert row.policy_rule_id == "tools-write-need-approval"
        assert verify_db(conn).ok
    result = httpx.get("http://127.0.0.1:8003/v1/audit/verify", timeout=30).json()
    assert result["ok"] and result["rows_checked"] >= 1


def test_tampering_is_detected(governor: Governor, owner_engine: Engine) -> None:
    governor.decide("agent:itest", "llm.chat", "fast", run_id="run_tamper")
    with owner_engine.connect() as conn:
        trans = conn.begin()
        conn.execute(
            text(
                "UPDATE audit_log SET decision = 'deny' WHERE seq = (SELECT max(seq) FROM audit_log WHERE run_id = 'run_tamper')"
            )
        )
        result = verify_db(conn)
        trans.rollback()
    assert not result.ok and result.reason == "row content does not match row_hash"


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_log SET decision = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"]
)
def test_app_role_cannot_change_audit(app_engine: Engine, statement: str) -> None:
    with app_engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
        conn.execute(text(statement))


def test_event_survives_relay_restart_and_is_deduped(governor: Governor, app_engine: Engine) -> None:
    compose("stop", "relay")
    try:
        with app_engine.begin() as conn:
            event = emit(
                conn,
                governor.schemas,
                "hapm/itest",
                "run.step",
                {"run_id": "run_relay", "task_id": "t", "step": "s", "kind": "test", "status": "ok"},
                subject="run_relay",
            )
        with app_engine.connect() as conn:
            pending = conn.execute(text("SELECT published_at FROM outbox WHERE id = :i"), {"i": event.id}).scalar_one()
            assert pending is None
    finally:
        compose("start", "relay")

    async def consume() -> list[dict[str, object]]:
        consumer = AIOKafkaConsumer("runs.events", bootstrap_servers=KAFKA, auto_offset_reset="earliest", group_id=None)
        await consumer.start()
        found: list[dict[str, object]] = []
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not found:
                batch = await consumer.getmany(timeout_ms=1000)
                for messages in batch.values():
                    for m in messages:
                        payload = json.loads(m.value)
                        if payload["id"] == event.id:
                            found.append(payload)
        finally:
            await consumer.stop()
        return found

    found = asyncio.run(consume())
    assert len(found) >= 1, "event was not published after relay restart"
    with app_engine.begin() as conn:
        assert already_processed(conn, "itest-consumer", event.id) is False
        assert already_processed(conn, "itest-consumer", event.id) is True
