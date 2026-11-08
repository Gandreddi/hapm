"""POST /v1/run: run a command on a snapshot plus proposed edits; only results leave the sandbox."""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from hapm_core.auth import Principal, TokenVerifier, principal_dependency
from hapm_core.settings import CoreSettings
from hapm_telemetry import configure_logging, configure_tracing, health_router
from pydantic import BaseModel, Field

from hapm_sandbox.docker_api import DockerClient, DockerError, Limits


class SandboxSettings(CoreSettings):
    service_name: str = "sandbox"
    docker_socket: str = "/var/run/docker.sock"
    allowed_images: list[str] = ["python:3.12.15-slim-trixie"]


class RunRequest(BaseModel):
    image: str = Field(max_length=200)
    command: str = Field(min_length=1, max_length=1000)
    files: dict[str, str]
    cpus: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=512, ge=128, le=8192)
    pids_limit: int = Field(default=256, ge=32, le=4096)
    timeout_seconds: int = Field(default=120, ge=5, le=3600)


def create_app(settings: SandboxSettings | None = None, docker: DockerClient | None = None) -> FastAPI:
    settings = settings or SandboxSettings()
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    client = docker or DockerClient(settings.docker_socket)
    verifier = TokenVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    admin = principal_dependency(lambda: verifier, "admin")
    app = FastAPI(title="HAPM sandbox", version="0.1.0")

    async def docker_ready() -> bool:
        return client.ping()

    app.include_router(health_router({"docker": docker_ready}))

    @app.post("/v1/run")
    def run(body: RunRequest, user: Annotated[Principal, Depends(admin)]) -> dict[str, Any]:
        if body.image not in settings.allowed_images:
            raise HTTPException(403, f"image {body.image} is not in the sandbox allow-list")
        if not body.files or len(body.files) > 5000 or sum(len(v) for v in body.files.values()) > 50_000_000:
            raise HTTPException(422, "files must hold 1-5000 entries and at most 50 MB")
        if any(p.startswith("/") or ".." in p.split("/") for p in body.files):
            raise HTTPException(422, "file paths must be relative and stay inside the work directory")
        try:
            return client.run(
                body.image,
                body.command,
                body.files,
                Limits(body.cpus, body.memory_mb, body.pids_limit, body.timeout_seconds),
            )
        except DockerError as exc:
            raise HTTPException(502, f"sandbox failed: {exc}") from exc

    return app
