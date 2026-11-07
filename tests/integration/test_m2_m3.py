"""M2 and M3 DONE checks against the running stack."""

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_mcp_client import ToolCallError, ToolClient, ToolRegistry
from hapm_memory import GatewayEmbedder, MemoryAccessDenied, MemoryScope, MemoryStore
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = "http://127.0.0.1:8001"
ALL_TOOLS = ["create_story", "add_comment", "list_stories", "read_file", "open_pull_request", "list_pull_requests"]


def ctx(**kw: str) -> CallerContext:
    run = f"run_{uuid.uuid4().hex[:10]}"
    base = {"agent_id": "itest", "team_id": "core", "run_id": run, "task_id": f"task_{run}"}
    return CallerContext(**{**base, **kw})


def test_gateway_call_records_usage_audit_and_event(app_engine: Engine) -> None:
    c = ctx()
    resp = httpx.post(
        f"{GATEWAY}/v1/chat",
        json={"model": "fast", "messages": [{"role": "user", "content": "summarise: hello world"}]},
        headers=c.headers(),
        timeout=30,
    )
    assert resp.status_code == 200, resp.text
    with app_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM llm_usage WHERE run_id = :r"), {"r": c.run_id}).scalar() == 1
        actions = set(conn.execute(text("SELECT action FROM audit_log WHERE run_id = :r"), {"r": c.run_id}).scalars())
        assert {"policy.llm.chat", "llm.chat.completed"} <= actions
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE type = 'llm.usage' AND payload->>'subject' = :r"),
                {"r": c.run_id},
            ).scalar()
            == 1
        )


@pytest.fixture
def memory(governor: Governor) -> MemoryStore:
    return MemoryStore(governor, GatewayEmbedder(GATEWAY))


def test_memory_write_read_search_delete(memory: MemoryStore) -> None:
    c = ctx()
    allowed = ["run", "agent"]
    a = memory.write(c, MemoryScope.RUN, "release notes for version two", allowed)
    memory.write(c, MemoryScope.RUN, "lunch menu pasta", allowed, ttl_seconds=3600)
    assert len(memory.read(c, MemoryScope.RUN, allowed)) == 2
    hits = memory.search(c, MemoryScope.RUN, "version two release", allowed, k=1)
    assert hits[0].id == a and hits[0].distance is not None
    assert memory.delete(c, MemoryScope.RUN, a, allowed) is True
    assert len(memory.read(c, MemoryScope.RUN, allowed)) == 1


def test_denied_scope_is_refused_and_audited(memory: MemoryStore, app_engine: Engine) -> None:
    c = ctx()
    with pytest.raises(MemoryAccessDenied):
        memory.read(c, MemoryScope.TASK, ["run"])
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT decision, policy_rule_id FROM audit_log WHERE run_id = :r AND action = 'memory.read'"),
            {"r": c.run_id},
        ).one()
    assert (row.decision, row.policy_rule_id) == ("deny", "agent-scope-allow-list")


@pytest.fixture
def tools(governor: Governor) -> ToolClient:
    return ToolClient(ToolRegistry.load(ROOT / "connectors/tools.yaml"), governor)


def test_write_tool_requires_approval_then_runs_idempotently(tools: ToolClient, app_engine: Engine) -> None:
    c = ctx()
    args = {"title": f"Story {c.run_id}", "body": "from itest"}

    async def run() -> None:
        first = await tools.call(c, "create_story", args, ALL_TOOLS)
        assert first.status == "approval_required" and first.approval_policy_id == "default_write"
        key = f"{c.run_id}:create_story"
        done = await tools.call(c, "create_story", args, ALL_TOOLS, approved=True, idempotency_key=key)
        again = await tools.call(c, "create_story", args, ALL_TOOLS, approved=True, idempotency_key=key)
        assert done.status == "ok" and done.output["created"] is True
        assert again.output["id"] == done.output["id"] and again.output["created"] is False

    asyncio.run(run())
    with app_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM tracker_stories WHERE title = :t"), {"t": args["title"]}).scalar()
            == 1
        )


def test_read_tool_and_eval_dry_run(tools: ToolClient, app_engine: Engine) -> None:
    async def run() -> None:
        read = await tools.call(ctx(), "read_file", {"path": "README.md"}, ALL_TOOLS)
        assert read.status == "ok" and "demo-service" in read.output["content"]
        dry = await tools.call(ctx(mode="eval"), "create_story", {"title": "dry", "body": "x"}, ALL_TOOLS)
        assert dry.status == "dry_run"

    asyncio.run(run())
    with app_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tracker_stories WHERE title = 'dry'")).scalar() == 0


def test_limits_schema_and_allow_list_enforced(tools: ToolClient) -> None:
    async def run() -> None:
        with pytest.raises(ToolCallError) as e1:
            await tools.call(ctx(), "create_story", {"title": "x", "body": "y"}, ["read_file"])
        assert e1.value.code == "not_allowed"
        with pytest.raises(ToolCallError) as e2:
            await tools.call(
                ctx(),
                "create_story",
                {"title": "x", "body": "y" * 20000},
                ALL_TOOLS,
                approved=True,
                idempotency_key="k",
            )
        assert e2.value.code == "too_large"
        with pytest.raises(ToolCallError) as e3:
            await tools.call(ctx(), "read_file", {"wrong": 1}, ALL_TOOLS)
        assert e3.value.code == "invalid_arguments"
        with pytest.raises(ToolCallError) as e4:
            await tools.call(ctx(), "create_story", {"title": "x", "body": "y"}, ALL_TOOLS, approved=True)
        assert e4.value.code == "idempotency_key_required"

    asyncio.run(run())
