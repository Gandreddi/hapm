"""Agent memory in three scopes with semantic search (pgvector, gateway embeddings)."""

from hapm_memory.store import (
    Embedder,
    GatewayEmbedder,
    MemoryAccessDenied,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
)

__all__ = [
    "Embedder",
    "GatewayEmbedder",
    "MemoryAccessDenied",
    "MemoryRecord",
    "MemoryScope",
    "MemoryStore",
]
