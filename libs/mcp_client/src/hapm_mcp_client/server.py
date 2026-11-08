"""Shared setup for HAPM MCP servers: stateless streamable HTTP, DNS-rebinding protection, health routes."""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse


def build_server(name: str, allowed_hosts: list[str], ready: Callable[[], bool]) -> FastMCP:
    mcp = FastMCP(
        name,
        host="0.0.0.0",  # noqa: S104 - container port, published to 127.0.0.1 only
        port=8000,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts),
    )

    @mcp.custom_route("/healthz", methods=["GET"])  # type: ignore[misc]
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])  # type: ignore[misc]
    async def readyz(request: Request) -> JSONResponse:
        try:
            ok = ready()
        except Exception:
            ok = False
        return JSONResponse({"status": "ok" if ok else "not_ready"}, status_code=200 if ok else 503)

    return mcp
