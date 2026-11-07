from fastapi import FastAPI
from fastapi.testclient import TestClient
from hapm_telemetry import health_router


async def passing() -> bool:
    return True


async def failing() -> bool:
    return False


async def raising() -> bool:
    raise RuntimeError("boom")


def client(**checks) -> TestClient:  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(health_router(checks))
    return TestClient(app)


def test_healthz_is_ok() -> None:
    assert client().get("/healthz").json() == {"status": "ok"}


def test_readyz_ok_when_all_checks_pass() -> None:
    response = client(db=passing).get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"db": "ok"}


def test_readyz_503_when_a_check_fails_or_raises() -> None:
    response = client(db=passing, cache=failing, bus=raising).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"db": "ok", "cache": "failed", "bus": "failed"},
    }
