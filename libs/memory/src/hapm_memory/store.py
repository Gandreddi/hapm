"""Memory store. Every access checks the agent's allowed scopes and policy, and is audited.

Scopes: run (short-term, keyed by run_id), task (shared by a task or team, keyed by task_id),
agent (long-term, keyed by agent_id).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import httpx
from hapm_core.audit import AuditEntry
from hapm_core.context import CallerContext
from hapm_core.governance import Governor
from hapm_core.ids import new_id
from sqlalchemy import text

SCOPE_RULE = "agent-scope-allow-list"


class MemoryScope(StrEnum):
    RUN = "run"
    TASK = "task"
    AGENT = "agent"


class MemoryAccessDenied(PermissionError):
    pass


@dataclass
class MemoryRecord:
    id: str
    scope: str
    content: str
    metadata: dict[str, Any]
    created_by: str
    created_at: datetime
    source_run_id: str | None
    expires_at: datetime | None
    distance: float | None = None


class Embedder(Protocol):
    def embed(self, ctx: CallerContext, texts: list[str]) -> list[list[float]]: ...


class GatewayEmbedder:
    def __init__(self, gateway_url: str, alias: str = "embed", timeout: float = 30) -> None:
        self._url = f"{gateway_url.rstrip('/')}/v1/embeddings"
        self._alias = alias
        self._timeout = timeout

    def embed(self, ctx: CallerContext, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            self._url, json={"model": self._alias, "input": texts}, headers=ctx.headers(), timeout=self._timeout
        )
        resp.raise_for_status()
        return [item["embedding"] for item in sorted(resp.json()["data"], key=lambda i: i["index"])]


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class MemoryStore:
    def __init__(self, governor: Governor, embedder: Embedder) -> None:
        self.governor = governor
        self.embedder = embedder

    def _scope_key(self, ctx: CallerContext, scope: MemoryScope) -> str:
        key = {MemoryScope.RUN: ctx.run_id, MemoryScope.TASK: ctx.task_id, MemoryScope.AGENT: ctx.agent_id}[scope]
        if not key:
            raise ValueError(f"{scope.value} scope needs a {scope.value} id in the caller context")
        return key

    def _authorize(self, ctx: CallerContext, op: str, scope: MemoryScope, allowed: list[str]) -> None:
        if scope.value not in allowed:
            entry = AuditEntry(
                actor=ctx.actor,
                action=f"memory.{op}",
                resource=scope.value,
                decision="deny",
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                policy_rule_id=SCOPE_RULE,
            )
            with self.governor.engine.begin() as conn:
                self.governor.audit(conn, entry)
                self._event(conn, ctx, op, scope, allowed=False)
            raise MemoryAccessDenied(f"agent {ctx.agent_id} may not {op} {scope.value} memory")
        decision = self.governor.decide(ctx.actor, f"memory.{op}", scope.value, run_id=ctx.run_id, task_id=ctx.task_id)
        if not decision.allowed:
            with self.governor.engine.begin() as conn:
                self._event(conn, ctx, op, scope, allowed=False)
            raise MemoryAccessDenied(f"policy {decision.rule_id} denied memory.{op} on {scope.value}")

    def _event(self, conn: Any, ctx: CallerContext, op: str, scope: MemoryScope, allowed: bool) -> None:
        self.governor.emit(
            conn,
            "run.memory_accessed",
            {
                "run_id": ctx.run_id or "",
                "operation": op,
                "scope": scope.value,
                "allowed": allowed,
                "agent_id": ctx.agent_id,
            },
            subject=ctx.run_id,
        )

    def _done(self, conn: Any, ctx: CallerContext, op: str, scope: MemoryScope, inputs: Any, outputs: Any) -> None:
        self.governor.audit(
            conn,
            AuditEntry(
                actor=ctx.actor,
                action=f"memory.{op}.completed",
                resource=scope.value,
                decision="allow",
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                inputs=inputs,
                outputs=outputs,
            ),
        )
        self._event(conn, ctx, op, scope, allowed=True)

    def write(
        self,
        ctx: CallerContext,
        scope: MemoryScope,
        content: str,
        allowed: list[str],
        *,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        if not content or len(content) > 50_000:
            raise ValueError("content must be 1-50000 characters")
        self._authorize(ctx, "write", scope, allowed)
        vector = self.embedder.embed(ctx, [content])[0]
        record_id = new_id("mem")
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        with self.governor.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_records (id, scope, scope_key, content, metadata, embedding, created_by, "
                    "source_run_id, expires_at) VALUES (:id, :scope, :key, :content, CAST(:meta AS jsonb), "
                    "CAST(:emb AS vector), :by, :run, :exp)"
                ),
                {
                    "id": record_id,
                    "scope": scope.value,
                    "key": self._scope_key(ctx, scope),
                    "content": content,
                    "meta": _json(metadata or {}),
                    "emb": _vector(vector),
                    "by": ctx.actor,
                    "run": ctx.run_id,
                    "exp": expires,
                },
            )
            self._done(conn, ctx, "write", scope, {"content": content, "metadata": metadata}, {"id": record_id})
        return record_id

    def read(
        self, ctx: CallerContext, scope: MemoryScope, allowed: list[str], *, limit: int = 50
    ) -> list[MemoryRecord]:
        self._authorize(ctx, "read", scope, allowed)
        with self.governor.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM memory_records WHERE scope = :scope AND scope_key = :key "
                    "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT :limit"
                ),
                {"scope": scope.value, "key": self._scope_key(ctx, scope), "limit": min(limit, 500)},
            ).all()
            records = [_record(r) for r in rows]
            self._done(conn, ctx, "read", scope, {"limit": limit}, {"ids": [r.id for r in records]})
        return records

    def search(
        self, ctx: CallerContext, scope: MemoryScope, query: str, allowed: list[str], *, k: int = 5
    ) -> list[MemoryRecord]:
        self._authorize(ctx, "search", scope, allowed)
        vector = self.embedder.embed(ctx, [query])[0]
        with self.governor.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT *, embedding <=> CAST(:q AS vector) AS distance FROM memory_records "
                    "WHERE scope = :scope AND scope_key = :key AND (expires_at IS NULL OR expires_at > now()) "
                    "ORDER BY distance LIMIT :k"
                ),
                {"q": _vector(vector), "scope": scope.value, "key": self._scope_key(ctx, scope), "k": min(k, 50)},
            ).all()
            records = [_record(r) for r in rows]
            self._done(conn, ctx, "search", scope, {"query": query, "k": k}, {"ids": [r.id for r in records]})
        return records

    def delete(self, ctx: CallerContext, scope: MemoryScope, record_id: str, allowed: list[str]) -> bool:
        self._authorize(ctx, "delete", scope, allowed)
        with self.governor.engine.begin() as conn:
            deleted = conn.execute(
                text("DELETE FROM memory_records WHERE id = :id AND scope = :scope AND scope_key = :key"),
                {"id": record_id, "scope": scope.value, "key": self._scope_key(ctx, scope)},
            ).rowcount
            self._done(conn, ctx, "delete", scope, {"id": record_id}, {"deleted": bool(deleted)})
        return bool(deleted)


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _record(row: Any) -> MemoryRecord:
    m = row._mapping
    return MemoryRecord(
        id=m["id"],
        scope=m["scope"],
        content=m["content"],
        metadata=m["metadata"],
        created_by=m["created_by"],
        created_at=m["created_at"],
        source_run_id=m["source_run_id"],
        expires_at=m["expires_at"],
        distance=float(m["distance"]) if "distance" in m and m["distance"] is not None else None,
    )
