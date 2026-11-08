from pathlib import Path

from hapm_core.settings import REPO_ROOT, CoreSettings

TASK_QUEUE = "hapm-agent-runs"


class OrchestratorSettings(CoreSettings):
    service_name: str = "orchestrator"
    gateway_url: str = "http://127.0.0.1:8001"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    task_queue: str = TASK_QUEUE
    tools_path: Path = REPO_ROOT / "connectors" / "tools.yaml"
    activity_threads: int = 8
    sandbox_url: str = "http://127.0.0.1:8105"
    oidc_token_url: str = "http://127.0.0.1:8006/token"  # noqa: S105 - a URL, not a secret
    service_client_id: str = "eval-service"
    service_client_secret: str = "eval-service-dev"  # noqa: S105 - dev stub value; Key Vault reference in cloud

    @property
    def checkpoint_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
