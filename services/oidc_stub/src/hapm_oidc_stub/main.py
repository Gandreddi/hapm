"""Discovery, JWKS and a token endpoint (password and client_credentials grants).

The signing key is generated at start-up and kept in memory, so tokens stop working when the stub
restarts. Services refetch the JWKS when they see an unknown key id.
"""

import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from hapm_telemetry import configure_logging, health_router
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USERS = Path(__file__).resolve().parents[2] / "users.yaml"


class StubSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAPM_", extra="ignore")

    oidc_issuer: str = "http://localhost:8006"
    oidc_audience: str = "hapm"
    oidc_users_path: Path = DEFAULT_USERS
    token_ttl_seconds: int = 3600
    log_level: str = "INFO"


def create_app(settings: StubSettings | None = None) -> FastAPI:
    settings = settings or StubSettings()
    configure_logging("oidc-stub", settings.log_level)
    directory = yaml.safe_load(settings.oidc_users_path.read_text(encoding="utf-8"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = uuid.uuid4().hex
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    issuer = settings.oidc_issuer.rstrip("/")

    app = FastAPI(title="HAPM dev OIDC stub", version="0.1.0")
    app.include_router(health_router())

    @app.get("/.well-known/openid-configuration")
    def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "jwks_uri": f"{issuer}/jwks",
            "token_endpoint": f"{issuer}/token",
            "grant_types_supported": ["password", "client_credentials"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    @app.get("/jwks")
    def jwks() -> dict[str, Any]:
        return {"keys": [public_jwk]}

    def issue(sub: str, roles: list[str]) -> dict[str, Any]:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": settings.oidc_audience,
            "sub": sub,
            "roles": roles,
            "iat": now,
            "exp": now + settings.token_ttl_seconds,
            "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
        return {"access_token": token, "token_type": "Bearer", "expires_in": settings.token_ttl_seconds}

    @app.post("/token")
    def token(
        grant_type: Annotated[str, Form()],
        username: Annotated[str | None, Form()] = None,
        password: Annotated[str | None, Form()] = None,
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        if grant_type == "password":
            user = directory.get("users", {}).get(username or "")
            if not user or not secrets.compare_digest(str(user["password"]), password or ""):
                raise HTTPException(401, "invalid username or password")
            return issue(str(username), list(user["roles"]))
        if grant_type == "client_credentials":
            client = directory.get("clients", {}).get(client_id or "")
            if not client or not secrets.compare_digest(str(client["secret"]), client_secret or ""):
                raise HTTPException(401, "invalid client")
            return issue(f"client:{client_id}", list(client["roles"]))
        raise HTTPException(400, "unsupported grant_type")

    return app
