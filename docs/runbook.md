# Runbook

## Prerequisites
- Docker Desktop (WSL2 backend) or Rancher Desktop in dockerd mode, GNU make, uv, Node LTS and pnpm.
- Check: `docker run --rm hello-world`, `make --version`, `uv --version`, `pnpm --version`.
- About 4 GB of free RAM for the stack (it uses roughly 2.5 GB when idle).

## First run
```bash
uv sync --all-packages
make check
make up
make demo
make verify
```
`make up` creates `.env` from `.env.example` if it is missing. All values in it are local
development values.

## Commands
| Command | What it does |
|---|---|
| `make up` | Build and start all 16 containers; fails unless every service is healthy |
| `make demo` | Eval, submit a task as requester, wait at the gate, approve as approver, confirm the story |
| `make verify` | Assert exit checks (a) to (f); prints PASS or FAIL for each and exits 1 on any failure |
| `make check` | ruff, `mypy --strict` on libs, unit tests |
| `make integration` | Integration tests against the running stack |
| `make coverage` | Unit and integration tests with the 80% libs coverage floor |
| `make portal-check` / `make e2e` | Portal type check and lint / Playwright smoke tests |
| `make ps`, `make logs` | Status and last 100 log lines |
| `make down` / `make clean` | Stop / stop and delete all data |

## Local endpoints (bound to 127.0.0.1 only)
| Service | Address |
|---|---|
| Portal (OneView-lite and inbox) | http://127.0.0.1:3000 |
| Gateway | http://127.0.0.1:8001 |
| Orchestrator API | http://127.0.0.1:8002/docs |
| Audit | http://127.0.0.1:8003/v1/audit |
| Eval | http://127.0.0.1:8004/docs |
| OneView API | http://127.0.0.1:8005/docs |
| OIDC stub | http://127.0.0.1:8006/.well-known/openid-configuration |
| Mock tracker / repo MCP | http://127.0.0.1:8101/mcp, http://127.0.0.1:8102/mcp |
| Temporal UI | http://127.0.0.1:8233 |

Dev sign-in users (from `services/oidc_stub/users.yaml`): `requester`, `approver`, `approver2`,
`admin`, `viewer`; each password is the user name followed by `-dev`.

## Adding an agent
1. Add `agents/<id>.yaml` (schema: `schemas/agent.schema.json`). No code change.
2. Add `agents/evals/<id>.yaml` with at least five cases.
3. Run `hapm eval run <id> <id>` with an admin token in `HAPM_TOKEN`. Until a suite passes for the
   exact config, the agent runs as `suggest_only`.

## Troubleshooting
| Symptom | Cause and fix |
|---|---|
| Python client hangs talking to `localhost` | Windows may try IPv6 first; Docker ports are bound to 127.0.0.1. Use `127.0.0.1`. |
| A new workspace package cannot be imported | Run `uv sync --all-packages --reinstall-package <name>` (seen with uv on Windows). |
| Docker Desktop upgrade fails while reading settings | `%APPDATA%\Docker\settings.json` is full of zero bytes; rename it so the installer recreates it. |
| Run stuck in `running` | `make logs`; check `orchestrator-worker`. Temporal UI shows the workflow history. |
| `make verify` (f) fails | The worker kill test needs Docker CLI access from the host shell. |

## Verified toolchain (2026-11-07)
Docker 29.9.2, Docker Compose v5.6.1, make 4.4.1, uv 0.11, Python 3.12.13, Node 20.17 (portal image
uses Node 22.12), pnpm 9.15.0.
