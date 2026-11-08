"""Client for the sandbox service. The worker authenticates with a service token from the identity provider."""

import time
from typing import Any

import httpx
from hapm_config import SandboxConfig


class ServiceToken:
    def __init__(self, token_url: str, client_id: str, client_secret: str) -> None:
        self.url, self.client_id, self.secret = token_url, client_id, client_secret
        self._token: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        if self._token is None or time.time() > self._expires - 60:
            resp = httpx.post(
                self.url,
                data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token, self._expires = str(data["access_token"]), time.time() + float(data.get("expires_in", 300))
        return self._token


class SandboxClient:
    def __init__(self, url: str, token: ServiceToken) -> None:
        self.url, self.token = url.rstrip("/"), token

    def run(self, image: str, command: str, files: dict[str, str], cfg: SandboxConfig) -> dict[str, Any]:
        body = {
            "image": image,
            "command": command,
            "files": files,
            "cpus": cfg.cpus,
            "memory_mb": cfg.memory_mb,
            "pids_limit": cfg.pids_limit,
            "timeout_seconds": cfg.timeout_seconds,
        }
        try:
            resp = httpx.post(
                f"{self.url}/v1/run",
                json=body,
                headers={"Authorization": f"Bearer {self.token()}"},
                timeout=cfg.timeout_seconds + 60,
            )
        except httpx.TransportError as exc:
            raise RuntimeError(f"sandbox unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise RuntimeError(f"sandbox error {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return dict(resp.json())
