# ADR-0017: Sandbox containers started through the Docker Engine API

- Status: accepted
- Date: 2026-11-08

## Context
The Developer agent runs repository tests on proposed changes. That code is untrusted.

## Decision
services/sandbox starts one throwaway container per attempt through the Docker Engine API over the Unix socket (httpx, no new dependency): no network (NetworkMode none), user nobody, all capabilities dropped, no-new-privileges, memory, CPU and PID limits, a private tmpfs, an image allow-list, and the container is force-removed afterwards. Files go in as a tar; only exit code and output come back. The service itself runs as root solely to reach the socket and requires an admin token.

## Options considered
Running tests in the worker (no isolation); gVisor or Kata (better isolation, not available on Docker Desktop); Kubernetes Jobs (AKS design, backlog).

## Consequences
Socket access is powerful: the sandbox service must stay internal. For AKS, replace with Kubernetes Jobs in a locked-down namespace with gVisor or Kata (backlog).
