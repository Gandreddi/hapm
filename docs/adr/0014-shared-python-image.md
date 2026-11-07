# ADR-0014: One Python image for all services

- Status: accepted
- Date: 2026-11-07

## Context
Nine Python processes share the same workspace and libraries.

## Decision
`deploy/docker/python.Dockerfile` builds the whole uv workspace once (non-root user, no dev dependencies); compose and Helm choose each service by its command.

## Options considered
One image per service (slower builds, more to scan).

## Consequences
Images are larger than strictly needed per service, but builds and scans happen once.
