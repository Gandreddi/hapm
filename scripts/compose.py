"""Run docker compose with the project's compose file and .env, creating .env if missing."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "deploy" / "compose" / "compose.yaml"
ENV_FILE = ROOT / ".env"


def ensure_env() -> None:
    if not ENV_FILE.exists():
        shutil.copyfile(ROOT / ".env.example", ENV_FILE)
        print("Created .env from .env.example (local development values).")


def compose(*args: str) -> int:
    if shutil.which("docker") is None:
        print("docker was not found on PATH. Install Docker Desktop or Rancher Desktop first.")
        return 127
    ensure_env()
    cmd = ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]
    return subprocess.call(cmd, cwd=ROOT)  # noqa: S603


if __name__ == "__main__":
    sys.exit(compose(*sys.argv[1:]))
