"""Typed gateway settings, read from HAPM_* environment variables."""

from pathlib import Path

from hapm_core.settings import CoreSettings
from pydantic import Field

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"


def default_providers_path() -> Path:
    """config/providers.yaml on the office laptop, else the committed example (Fake provider)."""
    real = CONFIG_DIR / "providers.yaml"
    return real if real.exists() else CONFIG_DIR / "providers.example.yaml"


class GatewaySettings(CoreSettings):
    service_name: str = "gateway"
    config_path: Path = Field(default_factory=default_providers_path)
    redis_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    ready_timeout_seconds: float = 2.0
