"""Liveness (/healthz) and readiness (/readyz) endpoints."""

from collections.abc import Awaitable, Callable, Mapping

from fastapi import APIRouter
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[bool]]


def health_router(checks: Mapping[str, ReadinessCheck] | None = None) -> APIRouter:
    """Build a router; /readyz returns 503 if any dependency check fails or raises."""
    router = APIRouter(tags=["health"])
    registered = dict(checks or {})

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        results: dict[str, str] = {}
        for name, check in registered.items():
            try:
                results[name] = "ok" if await check() else "failed"
            except Exception:
                results[name] = "failed"
        ready = all(value == "ok" for value in results.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "not_ready", "checks": results},
        )

    return router
