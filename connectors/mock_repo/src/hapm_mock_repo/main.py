"""Tools: read_file and list_pull_requests (read), open_pull_request (write, idempotent)."""

from typing import Any

from hapm_core.db import make_engine
from hapm_core.ids import new_id
from hapm_core.settings import CoreSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Engine, text
from starlette.requests import Request
from starlette.responses import JSONResponse

FILES = {
    "README.md": "# demo-service\n\nA small demo service used by the HAPM mock repository.\n",
    "src/app.py": "def handler(event):\n    return {'status': 'ok'}\n",
}


class RepoSettings(CoreSettings):
    service_name: str = "mock-repo"
    mcp_allowed_hosts: list[str] = ["mock-repo:8000", "127.0.0.1:8102", "localhost:8102"]


def build_server(engine: Engine, settings: RepoSettings) -> FastMCP:
    mcp = FastMCP(
        "hapm-mock-repo",
        host="0.0.0.0",  # noqa: S104 - container port, published to 127.0.0.1 only
        port=8000,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=settings.mcp_allowed_hosts
        ),
    )

    @mcp.tool()
    def read_file(path: str) -> dict[str, Any]:
        """Read a file from the demo repository."""
        if path not in FILES:
            raise ValueError(f"file {path} not found")
        return {"path": path, "content": FILES[path]}

    @mcp.tool()
    def open_pull_request(repo: str, title: str, branch: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Open a pull request. Repeating a call with the same idempotency_key returns the same PR."""
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO repo_pull_requests (id, repo, title, branch, body, idempotency_key) VALUES (:i, :r, :t, :b, :d, :k) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"i": new_id("pr"), "r": repo, "t": title, "b": branch, "d": body, "k": idempotency_key},
            )
            row = conn.execute(
                text("SELECT id, title FROM repo_pull_requests WHERE idempotency_key = :k"), {"k": idempotency_key}
            ).one()
        return {"id": row.id, "title": row.title}

    @mcp.tool()
    def list_pull_requests(limit: int = 20) -> dict[str, Any]:
        """List the most recent pull requests."""
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, repo, title, branch FROM repo_pull_requests ORDER BY created_at DESC LIMIT :l"),
                {"l": max(1, min(limit, 100))},
            ).all()
        return {"pull_requests": [dict(r._mapping) for r in rows]}

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
    settings = RepoSettings()
    from hapm_telemetry import configure_logging

    configure_logging(settings.service_name, settings.log_level)
    build_server(make_engine(settings.database_url), settings).run(transport="streamable-http")


if __name__ == "__main__":
    main()
