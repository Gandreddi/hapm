# Progress

All commands were run on the development machine (Windows 11, Docker 29.9.2) on 2026-11-07.
Output is trimmed; nothing below is reported unless it was run.

## M0 Foundation
- Repository layout, uv and pnpm workspaces, Makefile and scripts, `libs/telemetry`, compose, CI, ADRs.
- `make up`: all infrastructure healthy. `/healthz` and `/readyz` returned HTTP 200.
- Fix: Temporal image tag `1.1.2` does not exist; pinned `1.10.0`.

## M1 Governance base
- `libs/core` (settings, ids, caller context, hashing, artifacts, audit chain, Governor, auth, migrations, CLI),
  `libs/policy`, `libs/events` (envelope, schemas, outbox, dedupe), `services/audit`, `services/relay`,
  10 event schemas, least-privilege roles.
- `pytest tests/integration/test_m1_governance.py`: 6 passed (decision audited and chain verifies; tampering
  detected; hapm_app refused UPDATE, DELETE, TRUNCATE; event survives relay restart and is deduped).

## M2 LLM gateway
- Fake, Azure OpenAI and Anthropic adapters; alias routing with fallback; Redis budgets; usage, audit and
  `llm.usage` events; redaction hook; retention purge.
- `pytest tests/unit`: 41 passed (includes routing, fallback on error and on timeout, 429 budget, redaction,
  error mapping, adapters against mocked HTTP).

## M3 Memory and tools
- `libs/memory` (three scopes, pgvector search through gateway embeddings), `libs/mcp_client`, mock tracker
  and mock repo MCP servers.
- `pytest tests/integration` at that point: 12 passed.
- Fix: MCP errors were wrapped in an ExceptionGroup and retried; now raised after the session closes.

## M4 Orchestrator and M5 approvals
- Agent YAML and schema, `agent_run` workflow, LangGraph graph with check/act nodes, Postgres checkpoints,
  activities, worker, API (tasks, runs, cancel, approvals, memory), OIDC stub.
- `pytest tests/integration/test_m4_m5_orchestrator.py`: 9 passed in 64s (approve, reject, edit, identity
  rules, timeout -> rejected, cancel/approve race, worker killed mid-run and while waiting, bad input).

## M6 Evaluation
- Suite format, shared scorers, Fake judge, eval service and CLI, eval mode, autonomy gating, online scoring.
- `pytest tests/integration/test_m6_eval.py`: 5 passed (six-case suite aggregate 1.0; failing suite blocks
  autonomy and passing suite restores it; hand-edited config is clamped; live run scored online; admin only).

## M7 OneView-lite and portal
- `services/oneview` (consumer, read model, REST, SSE); Next.js portal (login, runs, live timeline, evals,
  approval inbox).
- `pytest tests/integration/test_m7_oneview.py`: 3 passed. `pnpm exec playwright test`: 3 passed.
  `pnpm typecheck` and `pnpm lint`: clean. `pnpm audit --prod`: no known vulnerabilities.

## M8 Exit
From a clean start (`make clean`, `make up`):

```
make demo
[2] Running the dummy_echo eval suite (gates autonomy)
    aggregate 1.0 (threshold 0.8), passed=True, cases=6
[4] OneView shows waiting_approval with proposed action: {... "tool": "create_story"}
[5] apr_run_3ce6..._create_story approved
[6] run state: succeeded; story in mock tracker: True

make verify
PASS (a) run state is succeeded
PASS (b) audit rows for every LLM call, memory access, tool call, policy decision and approval; chain verifies
         13 audit rows (2 LLM, 5 policy, memory, tool, approvals); chain verified over 75 rows
PASS (c) the same task rejected creates no story
PASS (d) the eval suite ran and meets its threshold
         suite dummy_echo scored 1.0 (threshold 0.8); autonomy act_with_approval
PASS (e) the OneView-lite API shows the complete timeline
         23 timeline events; states queued -> running -> waiting_approval -> succeeded
PASS (f) killing the worker mid-run and restarting it still completes the run
6/6 checks passed
```

- `make check`: ruff clean (84 files formatted), `mypy --strict` clean on 31 lib files, unit tests pass.
- All 29 integration tests together: 29 passed in 1m45s.
- `make coverage` first run: 84.64% but 1 failed, 69 passed in 64 minutes. Second run: 1 failed
  (`test_live_runs_are_scored_online`, a test race: the online score is written just after the terminal state).
  After making that test wait for the score row: **70 passed in 1m42s, libs coverage 84.25%** (floor 80%).
- Final run from a wiped stack after all fixes: `make check` (ruff clean, 86 files formatted, mypy clean on
  31 files, 41 unit tests), `make up` all healthy, `make demo` succeeded, `make verify` 6/6 passed,
  Playwright 3 passed.
- gitleaks (zricethezav/gitleaks:v8.21.2, directory scan): 9 findings, all in `apps/portal/.next` build
  output (Next.js preview keys), which is git-ignored; none in source.
- Helm (alpine/helm:3.16.3): `helm lint` 1 chart linted, 0 failed; `helm template` renders 12 objects.
- Terraform (hashicorp/terraform:1.9.8): `validate` succeeded; `fmt -check` clean.

### Not verified
- CI on GitHub (no remote), Trivy, gitleaks and pip-audit (CI only).
- Live Azure OpenAI and Anthropic calls (no credentials).
- Any cloud deployment.
- No commits (git identity).

## First CI run and fixes (2026-11-07)
- Run 1 on GitHub: infra passed; check, portal and secrets failed, exit-demo skipped.
  - Portal audit: pnpm ran without `--ignore-workspace` and found no lockfile. Fixed in CI.
  - Python audit: pip-audit tried to re-resolve the export and failed. With a hashed export and
    `--no-deps --disable-pip` it ran and found 8 real advisories: starlette 0.41.3 (7) and
    opentelemetry-api 1.29.0 (1).
  - gitleaks: the unpinned action failed; the pinned v8.21.2 scan of the full git history locally
    reports 0 findings. CI now runs the same pinned image.
- Upgraded FastAPI 0.115.6 -> 0.162.0 (Starlette 0.41.3 -> 1.10.0) and OpenTelemetry 1.29 -> 1.45.2.
  pip-audit: no known vulnerabilities.
- Retest after the upgrade from a wiped stack: make check passed (41 unit tests), make demo succeeded,
  make verify 6/6, make coverage 70 passed at 84.25%, Playwright 3 passed.
