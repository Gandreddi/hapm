"""OIDC bearer-token checks. Identity always comes from a verified token, never the request body."""

from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

ROLES = ("viewer", "approver", "admin")


class Principal(BaseModel):
    sub: str
    roles: list[str]

    def has_any(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._jwks = jwt.PyJWKClient(jwks_url or f"{self.issuer}/jwks", cache_keys=True)

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
        roles = claims.get("roles", [])
        if not isinstance(roles, list):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "roles claim must be a list")
        return Principal(sub=str(claims["sub"]), roles=[str(r) for r in roles])


_bearer = HTTPBearer(auto_error=False)


def principal_dependency(get_verifier: Callable[[], TokenVerifier], *roles: str) -> Callable[..., Principal]:
    """FastAPI dependency: 401 without a valid token, 403 without one of the roles."""

    def dependency(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Principal:
        if creds is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
        principal = get_verifier().verify(creds.credentials)
        if roles and not principal.has_any(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires one of roles {list(roles)}")
        return principal

    return dependency
