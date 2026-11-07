# HAPM

Multi-agent engineering platform for the software development lifecycle. This repository holds the
Phase 0 Core: an LLM gateway, a durable orchestrator, memory, MCP tools, policy and a tamper-evident
audit log, an event bus, an evaluation harness, human approval gates and OneView-lite. One harmless
agent, `dummy_echo`, proves every path.

## Quick start
```bash
uv sync --all-packages
make check
make up
make demo
make verify
```
Then open http://127.0.0.1:3000 and sign in as `viewer` / `viewer-dev` (dev-only test user).

- [docs/runbook.md](docs/runbook.md): prerequisites, commands, endpoints, troubleshooting
- [docs/architecture.md](docs/architecture.md): components, run sequence, states, data model
- [docs/adr/](docs/adr/): decisions
- [docs/progress.md](docs/progress.md): what was verified, with real command output
