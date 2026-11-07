"""Build the shared Core objects from settings."""

from hapm_events import SchemaRegistry
from hapm_policy import PolicyEngine

from hapm_core.artifacts import make_artifact_store
from hapm_core.db import make_engine
from hapm_core.governance import Governor
from hapm_core.settings import CoreSettings


def build_governor(settings: CoreSettings) -> Governor:
    return Governor(
        engine=make_engine(settings.database_url),
        policy=PolicyEngine.from_file(settings.policy_path),
        artifacts=make_artifact_store(settings.blob_connection_string, settings.blob_container),
        schemas=SchemaRegistry(settings.schemas_dir),
        source=f"hapm/{settings.service_name}",
    )
