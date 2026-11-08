"""Minimal Docker Engine API client over the Unix socket (no extra dependency, no Docker CLI)."""

import io
import struct
import tarfile
import time
from dataclasses import dataclass
from typing import Any

import httpx

API = "/v1.43"
SANDBOX_UID = 65534  # nobody


class DockerError(Exception):
    pass


@dataclass
class Limits:
    cpus: float
    memory_mb: int
    pids_limit: int
    timeout_seconds: int


def make_tar(files: dict[str, str], root: str = "work") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        directory = tarfile.TarInfo(root)
        directory.type, directory.mode, directory.uid, directory.gid = tarfile.DIRTYPE, 0o755, SANDBOX_UID, SANDBOX_UID
        tar.addfile(directory)
        dirs: set[str] = set()
        for path in sorted(files):
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(f"{root}/{d}")
                    info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, SANDBOX_UID, SANDBOX_UID
                    tar.addfile(info)
            data = files[path].encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size, info.mode, info.uid, info.gid = len(data), 0o644, SANDBOX_UID, SANDBOX_UID
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def demux(raw: bytes) -> str:
    """Docker multiplexes stdout and stderr with 8-byte frame headers when there is no TTY."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4 : i + 8])[0]
        out.append(raw[i + 8 : i + 8 + size])
        i += 8 + size
    if i < len(raw):
        out.append(raw[i:])
    return b"".join(out).decode("utf-8", errors="replace")


def container_spec(image: str, command: str, limits: Limits) -> dict[str, Any]:
    return {
        "Image": image,
        "Cmd": ["sh", "-c", command],
        "WorkingDir": "/work",
        "User": f"{SANDBOX_UID}:{SANDBOX_UID}",
        "Env": ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"],
        "NetworkDisabled": True,
        "Labels": {"hapm.sandbox": "true"},
        "HostConfig": {
            "NetworkMode": "none",
            "Memory": limits.memory_mb * 1024 * 1024,
            "MemorySwap": limits.memory_mb * 1024 * 1024,
            "NanoCpus": int(limits.cpus * 1_000_000_000),
            "PidsLimit": limits.pids_limit,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,size=64m"},  # noqa: S108 - private tmpfs inside the throwaway container
        },
    }


class DockerClient:
    def __init__(self, socket_path: str = "/var/run/docker.sock", client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url="http://docker", timeout=30
        )

    def _ok(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code >= 400:
            raise DockerError(f"{resp.request.method} {resp.request.url.path}: {resp.status_code} {resp.text[:200]}")
        return resp

    def ping(self) -> bool:
        return self.http.get("/_ping").status_code == 200

    def ensure_image(self, image: str) -> None:
        if self.http.get(f"{API}/images/{image}/json").status_code == 200:
            return
        name, _, tag = image.partition(":")
        with self.http.stream(
            "POST", f"{API}/images/create", params={"fromImage": name, "tag": tag or "latest"}, timeout=600
        ) as r:
            self._ok(r)
            for _ in r.iter_bytes():
                pass

    def run(self, image: str, command: str, files: dict[str, str], limits: Limits) -> dict[str, Any]:
        self.ensure_image(image)
        cid = self._ok(self.http.post(f"{API}/containers/create", json=container_spec(image, command, limits))).json()[
            "Id"
        ]
        started = time.monotonic()
        try:
            self._ok(
                self.http.put(
                    f"{API}/containers/{cid}/archive",
                    params={"path": "/"},
                    content=make_tar(files),
                    headers={"Content-Type": "application/x-tar"},
                )
            )
            self._ok(self.http.post(f"{API}/containers/{cid}/start"))
            timed_out = False
            try:
                status = self._ok(
                    self.http.post(f"{API}/containers/{cid}/wait", timeout=limits.timeout_seconds + 5)
                ).json()
                exit_code: int | None = int(status.get("StatusCode", -1))
            except httpx.TimeoutException:
                timed_out, exit_code = True, None
                self.http.post(f"{API}/containers/{cid}/kill")
            logs = demux(self.http.get(f"{API}/containers/{cid}/logs", params={"stdout": 1, "stderr": 1}).content)
            return {
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output": logs[-8000:],
            }
        finally:
            self.http.delete(f"{API}/containers/{cid}", params={"force": "true", "v": "true"})
