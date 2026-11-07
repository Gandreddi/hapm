"""Full prompts, responses and tool payloads live in Blob storage, addressed by content hash."""

from typing import Any, Protocol

from hapm_core.hashing import canonical_json, sha256_hex


class ArtifactStore(Protocol):
    def put(self, value: Any) -> str: ...

    def get(self, digest: str) -> bytes | None: ...


class MemoryArtifactStore:
    """In-process store for tests and for running without Blob storage."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def put(self, value: Any) -> str:
        data = canonical_json(value).encode("utf-8")
        digest = sha256_hex(data)
        self.items[digest] = data
        return digest

    def get(self, digest: str) -> bytes | None:
        return self.items.get(digest)


class BlobArtifactStore:
    """Azure Blob (Azurite locally). Writes are idempotent because names are content hashes."""

    def __init__(self, connection_string: str, container: str) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient

        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container = self._service.get_container_client(container)
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass

    def put(self, value: Any) -> str:
        from azure.core.exceptions import ResourceExistsError

        data = canonical_json(value).encode("utf-8")
        digest = sha256_hex(data)
        try:
            self._container.upload_blob(name=digest, data=data, overwrite=False)
        except ResourceExistsError:
            pass
        return digest

    def get(self, digest: str) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return bytes(self._container.download_blob(digest).readall())
        except ResourceNotFoundError:
            return None


def make_artifact_store(connection_string: str | None, container: str) -> ArtifactStore:
    if connection_string:
        return BlobArtifactStore(connection_string, container)
    return MemoryArtifactStore()
