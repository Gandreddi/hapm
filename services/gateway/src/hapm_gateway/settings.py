"""Typed gateway settings, read from HAPM_* environment variables."""

from pathlib import Path

from hapm_core.settings import CoreSettings

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


class GatewaySettings(CoreSettings):
    service_name: str = "gateway"
    config_path: Path = DEFAULT_CONFIG
    redis_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    ready_timeout_seconds: float = 2.0
