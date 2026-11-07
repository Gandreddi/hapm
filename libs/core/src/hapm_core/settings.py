"""Settings shared by every service. Each service extends this with its own fields."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAPM_", extra="ignore")

    service_name: str = "hapm"
    log_level: str = "INFO"
    otel_endpoint: str | None = None
    database_url: str = "postgresql+psycopg://hapm_app:hapm-app-local@localhost:5432/hapm"
    blob_connection_string: str | None = None
    blob_container: str = "artifacts"
    policy_path: Path = REPO_ROOT / "policies" / "default.yaml"
    schemas_dir: Path = REPO_ROOT / "schemas" / "events"
    agents_dir: Path = REPO_ROOT / "agents"
    oidc_issuer: str = "http://localhost:8006"
    oidc_jwks_url: str | None = None
    oidc_audience: str = "hapm"
