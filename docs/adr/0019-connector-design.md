# ADR-0019: Jira and GitHub connectors: mock and real backends, limited tools

- Status: accepted
- Date: 2026-11-08

## Context
Connectors must be testable offline and safe against real systems.

## Decision
connectors/jira and connectors/github are MCP servers with two backends behind one interface: a mock (Postgres plus the fixture folder) and a real REST client (Jira Cloud v3 or Data Center v2; GitHub App or fine-grained PAT, github.com or Enterprise Server). Tools are prefixed (jira_*, github_*) to avoid name clashes. Jira tools only touch allowed projects; GitHub tools only touch onboarded repositories, push only to new hapm/* branches, and no merge, delete, force-push or settings tool exists (asserted by tests). Writes are idempotent (Jira via an idempotency label, GitHub via branch name and open-PR lookup). Real backends are tested against recorded responses with respx.

## Options considered
Vendor MCP servers (larger tool surface, harder to restrict).

## Consequences
Live behaviour is unverified until office credentials exist; recorded responses follow the public API shapes.
