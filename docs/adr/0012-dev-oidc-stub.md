# ADR-0012: Own dev-only OIDC stub instead of mock-oauth2-server

- Status: accepted
- Date: 2026-11-07

## Context
Local runs need SSO tokens with a roles claim for requester, approver, admin and viewer, plus a service identity for the eval runner.

## Decision
`services/oidc_stub` (about 100 lines of FastAPI and PyJWT) generates an RSA key at start-up and serves discovery, JWKS and a token endpoint with password and client_credentials grants, reading test identities from `services/oidc_stub/users.yaml`. Every API verifies RS256 signatures, issuer, audience and expiry. This deviates from the earlier plan to use mock-oauth2-server, because configuring its role claims per user proved harder to make deterministic than a small stub.

## Options considered
mock-oauth2-server; Keycloak (heavy on memory); Dex (no per-user group claims for static users).

## Consequences
The portal signs in with the password grant against the stub. Entra ID in the cloud will use the authorization code flow; that change is in the backlog. The stub and its users file must never be deployed.
