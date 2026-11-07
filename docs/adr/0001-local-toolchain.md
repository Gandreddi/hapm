# ADR-0001: Local toolchain

- Status: accepted
- Date: 2026-11-07

## Context
HAPM runs about fifteen services. The exit criterion requires `make up`, `make demo` and `make verify`
to work locally without a cloud account, on Windows development machines and on Linux CI.

## Decision
- A Docker-compatible engine (Docker Desktop, or Rancher Desktop in dockerd mode) with the WSL2 backend.
- GNU make as the entry point. Make targets call Python scripts in `scripts/`, so behaviour does not
  depend on whether the shell is cmd, PowerShell or bash.
- uv for Python 3.12 and pnpm for the frontend.

## Options considered
- Native installs without containers: Redpanda and Redis lack official Windows builds, and the
  Developer agent later needs container isolation for its sandbox.
- Working only inside WSL Ubuntu: closest to CI, but moves the repository off the D: drive.

## Consequences
- Docker Desktop needs a paid subscription in larger companies; Rancher Desktop is the free fallback.
- Code is copied into images, so code changes need a rebuild.
