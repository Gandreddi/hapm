# ADR-0002: Monorepo layout and workspaces

- Status: accepted
- Date: 2026-11-07

## Context
The Core, its services and every future agent share libraries and must change together.

## Decision
One repository with a uv workspace for Python packages (`libs/*`, `services/*`, `connectors/*`) and a
pnpm workspace for `apps/*`. Workspace members are listed explicitly, because uv rejects a matched
folder that has no `pyproject.toml`. Each package uses a `src/` layout and ships `py.typed`.

Two folders are added beyond the original layout: `services/oneview` (ADR-0010) and `services/relay`
(ADR-0005), plus `policies/` and `scripts/`.

## Consequences
One lock file and one CI pipeline. Services can import libs directly; libs must not import services.
