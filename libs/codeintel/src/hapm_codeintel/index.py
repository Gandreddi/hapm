"""Code index in Postgres/pgvector. Incremental: chunks of unchanged blobs are reused, not re-embedded."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from hapm_codeintel.snapshot import Snapshot, blob_sha

CHUNK_LINES = 40
EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class IndexStats:
    files: int
    chunks: int
    reused: int
    skipped: int


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def chunk_file(path: str, content: str) -> list[tuple[int, int, str]]:
    lines = content.splitlines()
    out = []
    for start in range(0, max(len(lines), 1), CHUNK_LINES):
        part = lines[start : start + CHUNK_LINES]
        out.append((start + 1, start + len(part) or 1, "\n".join(part)))
    return out


def index_snapshot(engine: Engine, snapshot: Snapshot, embed: EmbedFn) -> IndexStats:
    with engine.connect() as conn:
        done = conn.execute(
            text("SELECT files, chunks, reused, skipped FROM code_index_state WHERE repo = :r AND sha = :s"),
            {"r": snapshot.repo, "s": snapshot.sha},
        ).first()
    if done:
        return IndexStats(done.files, done.chunks, done.reused, done.skipped)
    chunks = reused = 0
    with engine.begin() as conn:
        for path, content in snapshot.files.items():
            blob = blob_sha(content)
            previous = conn.execute(
                text(
                    "SELECT start_line, end_line, content, language, embedding::text AS emb FROM code_chunks "
                    "WHERE repo = :r AND blob_sha = :b AND path = :p AND sha = (SELECT sha FROM code_chunks "
                    "WHERE repo = :r AND blob_sha = :b AND path = :p LIMIT 1)"
                ),
                {"r": snapshot.repo, "b": blob, "p": path},
            ).all()
            language = path.rsplit(".", 1)[-1]
            if previous:
                rows: list[tuple[int, int, str, str]] = [(p.start_line, p.end_line, p.content, p.emb) for p in previous]
                reused += len(rows)
            else:
                parts = chunk_file(path, content)
                vectors = embed([f"{path}\n{c}" for _, _, c in parts])
                rows = [(s, e, c, _vector(v)) for (s, e, c), v in zip(parts, vectors, strict=True)]
            for start, end, body, emb in rows:
                conn.execute(
                    text(
                        "INSERT INTO code_chunks (repo, sha, path, blob_sha, language, start_line, end_line, content, embedding) "
                        "VALUES (:r, :s, :p, :b, :l, :st, :en, :c, CAST(:e AS vector))"
                    ),
                    {
                        "r": snapshot.repo,
                        "s": snapshot.sha,
                        "p": path,
                        "b": blob,
                        "l": language,
                        "st": start,
                        "en": end,
                        "c": body,
                        "e": emb,
                    },
                )
                chunks += 1
        conn.execute(
            text(
                "INSERT INTO code_index_state (repo, sha, files, chunks, skipped, reused) VALUES (:r, :s, :f, :c, :k, :u) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "r": snapshot.repo,
                "s": snapshot.sha,
                "f": len(snapshot.files),
                "c": chunks,
                "k": len(snapshot.skipped),
                "u": reused,
            },
        )
    return IndexStats(len(snapshot.files), chunks, reused, len(snapshot.skipped))


def search_code(engine: Engine, repo: str, sha: str, query_vector: list[float], k: int = 5) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT path, start_line, end_line, content, embedding <=> CAST(:q AS vector) AS distance FROM code_chunks "
                "WHERE repo = :r AND sha = :s ORDER BY distance LIMIT :k"
            ),
            {"q": _vector(query_vector), "r": repo, "s": sha, "k": k},
        ).all()
    return [dict(r._mapping) for r in rows]
