# ADR-0011: Where checks and writers live

- Status: accepted
- Date: 2026-11-07

## Context
Several components could own policy checks, audit writes and memory endpoints.

## Decision
LLM policy checks run in the gateway, so callers cannot bypass them. Tool and memory checks run in `libs/mcp_client` and `libs/memory`. The audit writer and the policy-audit-event facade (`Governor`) live in `libs/core`; `services/audit` is read-only plus verification. Memory endpoints are hosted by the orchestrator API.

## Options considered
A central policy service called over HTTP (another network hop on every step).

## Consequences
Every service that writes audit rows needs the shared library and database access with the app role.
