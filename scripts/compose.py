"""Run `<engine> compose` with the project's compose file and .env, creating .env if missing.

The container engine is Docker or Podman: set HAPM_CONTAINER_CLI=docker|podman, or leave it unset
to use docker when it is on PATH and podman otherwise.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "deploy" / "compose" / "compose.yaml"
ENV_FILE = ROOT / ".env"


def container_cli() -> str | None:
    chosen = os.environ.get("HAPM_CONTAINER_CLI")
    if chosen:
        return chosen if shutil.which(chosen) else None
    return next((cli for cli in ("docker", "podman") if shutil.which(cli)), None)


def compose_cmd(*args: str) -> list[str]:
    cli = container_cli()
    if cli is None:
        raise FileNotFoundError("neither docker nor podman was found on PATH (or HAPM_CONTAINER_CLI is wrong)")
    return [cli, "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]


def ensure_env() -> None:
    if not ENV_FILE.exists():
        shutil.copyfile(ROOT / ".env.example", ENV_FILE)
        print("Created .env from .env.example (local development values).")


def compose(*args: str) -> int:
    try:
        cmd = compose_cmd(*args)
    except FileNotFoundError as exc:
        print(f"{exc}. Install Docker Desktop, Rancher Desktop or Podman Desktop first.")
        return 127
    ensure_env()
    return subprocess.call(cmd, cwd=ROOT)  # noqa: S603


if __name__ == "__main__":
    sys.exit(compose(*sys.argv[1:]))
