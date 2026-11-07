"""Tools: create_story, add_comment (write) and list_stories (read). Writes are idempotent."""

from typing import Any

from hapm_core.db import make_engine
from hapm_core.ids import new_id
from hapm_core.settings import CoreSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Engine, text
from starlette.requests import Request
from starlette.responses import JSONResponse


class TrackerSettings(CoreSettings):
    service_name: str = "mock-tracker"
    mcp_allowed_hosts: list[str] = ["mock-tracker:8000", "127.0.0.1:8101", "localhost:8101"]


def build_server(engine: Engine, settings: TrackerSettings) -> FastMCP:
    mcp = FastMCP(
        "hapm-mock-tracker",
        host="0.0.0.0",  # noqa: S104 - container port, published to 127.0.0.1 only
        port=8000,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=settings.mcp_allowed_hosts
        ),
    )

    @mcp.tool()
    def create_story(title: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Create a story. Repeating a call with the same idempotency_key returns the same story."""
        if not title.strip() or len(title) > 200:
            raise ValueError("title must be 1-200 characters")
        with engine.begin() as conn:
            existing = conn.execute(
                text("SELECT id, title FROM tracker_stories WHERE idempotency_key = :k"), {"k": idempotency_key}
            ).first()
            if existing:
                return {"id": existing.id, "title": existing.title, "created": False}
            story_id = new_id("story")
            conn.execute(
                text(
                    "INSERT INTO tracker_stories (id, title, body, idempotency_key) VALUES (:i, :t, :b, :k) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"i": story_id, "t": title, "b": body, "k": idempotency_key},
            )
            row = conn.execute(
                text("SELECT id, title FROM tracker_stories WHERE idempotency_key = :k"), {"k": idempotency_key}
            ).one()
        return {"id": row.id, "title": row.title, "created": row.id == story_id}

    @mcp.tool()
    def add_comment(story_id: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Add a comment to a story."""
        with engine.begin() as conn:
            if not conn.execute(text("SELECT 1 FROM tracker_stories WHERE id = :i"), {"i": story_id}).first():
                raise ValueError(f"story {story_id} not found")
            conn.execute(
                text(
                    "INSERT INTO tracker_comments (id, story_id, body, idempotency_key) VALUES (:i, :s, :b, :k) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"i": new_id("comment"), "s": story_id, "b": body, "k": idempotency_key},
            )
            row = conn.execute(
                text("SELECT id FROM tracker_comments WHERE idempotency_key = :k"), {"k": idempotency_key}
            ).one()
        return {"id": row.id, "story_id": story_id}

    @mcp.tool()
    def list_stories(limit: int = 20) -> dict[str, Any]:
        """List the most recent stories."""
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, title, body FROM tracker_stories ORDER BY created_at DESC LIMIT :l"),
                {"l": max(1, min(limit, 100))},
            ).all()
        return {"stories": [{"id": r.id, "title": r.title, "body": r.body} for r in rows]}

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(request: Request) -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "not_ready", "checks": {"postgres": "failed"}}, status_code=503)
        return JSONResponse({"status": "ok", "checks": {"postgres": "ok"}})

    return mcp


def main() -> None:
    settings = TrackerSettings()
    from hapm_telemetry import configure_logging

    configure_logging(settings.service_name, settings.log_level)
    build_server(make_engine(settings.database_url), settings).run(transport="streamable-http")


if __name__ == "__main__":
    main()
