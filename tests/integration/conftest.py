"""Integration tests run against the local stack started by make up."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from hapm_core.bootstrap import build_governor
from hapm_core.governance import Governor
from hapm_core.settings import CoreSettings
from sqlalchemy import Engine, create_engine

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    return os.environ.get(name, values.get(name, default))


APP_URL = f"postgresql+psycopg://hapm_app:{_env('HAPM_APP_DB_PASSWORD', 'hapm-app-local')}@127.0.0.1:5432/hapm"
OWNER_URL = f"postgresql+psycopg://hapm:{_env('POSTGRES_PASSWORD', 'hapm-local-dev')}@127.0.0.1:5432/hapm"
KAFKA = "127.0.0.1:19092"


def compose(*args: str) -> None:
    from scripts.compose import compose_cmd

    cmd = compose_cmd(*args)
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603, S607 - fixed docker compose command


@pytest.fixture(scope="session")
def app_engine() -> Iterator[Engine]:
    engine = create_engine(APP_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(OWNER_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def governor() -> Governor:
    key = _env("AZURITE_ACCOUNT_KEY", "")
    blob = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        f"AccountKey={key};BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    )
    return build_governor(CoreSettings(service_name="itest", database_url=APP_URL, blob_connection_string=blob))
