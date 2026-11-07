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

    @property
    def checkpoint_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
