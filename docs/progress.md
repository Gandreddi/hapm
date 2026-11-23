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

## Phase 1: Developer agent, M9 to M13 (2026-11-08, offline with mocks)

### M9 Configuration, providers and connectors
- libs/config (typed config files, env:/kv: references, example fallback), `hapm config check`,
  GitHub Models adapter, Jira and GitHub MCP connectors with mock and REST backends.
- `make config-check` on the examples: LIVE 0, MOCKED 4, MISSING 1 (data_approval); `--pilot` exits 2.
- Unit: 13 tests against recorded responses (Jira Cloud and Data Center, GitHub PAT and App token,
  push branch and PR, rate-limit retries, GitHub Models chat, embeddings and 401 mapping, allowed
  projects, onboarded repositories only, no merge/delete/force/settings tool).

### M10 Code context, scan and findings
- libs/codeintel: snapshot filtering (excluded, oversized, binary, secret-pattern files never read),
  Python analysers (9 rules), verified locations, fingerprints, dedupe against Jira, incremental
  pgvector index, autofixes for 4 rules.
- Fixture with 25 seeded defects (23 rule-detectable, 2 logic bugs). Detected 23: recall 0.92,
  precision 1.0, location accuracy 1.0. All 13 autofixes resolve their finding.

### M11 Sandbox and implementation
- services/sandbox via the Docker Engine API. Direct check: `LEAK [] NET blocked UID 65534`;
  unlisted image refused (403). Guardrails: denied paths, secrets, diff limit, test deletion flags.

### M12 Developer workflow, injection defence and evaluation
- General plugin and gate steps in agent YAML (ADR-0018); agents/developer.yaml with 3 gates.
- Developer eval suite: 5 cases, aggregate 1.0; agent unlocked to act_with_approval.
- `make dev-demo`: Gate 1 kept 1 of 3 findings -> ticket PAY-4; Gate 2 plan; sandbox fix
  "Ran 7 tests ... OK"; Gate 3 diff -> PR #1 from hapm/pay-4-...
- `make dev-verify` from a wiped stack: 10/10 passed (a-j).

### M13 Pilot
- `make pilot` refuses with the example config and lists the missing value. `make pilot-report`
  writes docs/pilot-report.md (mock data, labelled).

### Totals after Phase 1 (fresh stack)
- `make check`: ruff clean (116 files), mypy --strict clean (43 files), 63 unit tests.
- `make verify` 6/6, `make dev-verify` 10/10, `make coverage` 92 passed at 86.22%, Playwright 3/3.

### Not verified
- Real Jira, GitHub and GitHub Models (recorded responses only), AKS sandbox design.

## Change: GitHub Models retired (2026-11-19)
- GitHub retired GitHub Models on 2026-07-30, so the github_models adapter was removed (ADR-0020,
  superseding ADR-0016). The office route is Azure OpenAI models deployed in Azure AI Foundry.
- A config file naming github_models now fails with a message pointing to ADR-0020.
- Compose passed no Azure or Anthropic credentials to any container; they now go to the gateway only.

## Phase 2: Sentinel context agent (M14-M20), 2026-11-22

Built from ../HAPM_Phase2_Context_Agent_Prompt.md. Decisions: ADR-0021 (engines), ADR-0022 (store,
linkers, context API), ADR-0023 (LLM modes, local models, Copilot rule). Guide: docs/sentinel.md.

### M14 Repository fleet and snapshots
- `sentinel:` section in config/repos (enabled, tiers, node_budget, enrich, engines, llm, service_names);
  config/sentinel.example.yaml; four new fixture repositories (shared_lib, orders_service,
  inventory_service in Python; web_portal in TypeScript) plus payments_service.
- Readers: GitHub MCP connector (read tools only; anything else raises) and fixture folders, both giving
  the same SHA. Mock GitHub reports commit messages from `<fixture>.commits.yaml`.
- `sentinel build` (incremental by default, `--force`, `--dry-run`); `sentinel_fleet` / `sentinel_repo`
  Temporal workflows on the orchestrator worker. Temporal run through the connector: 5/5 succeeded, all
  `exact`, 16 cross-repository links. `hapm repos discover --org` writes disabled drafts only.

### M15 Extraction engines and storage
- graphify 0.9.115 and SCIP 0.5.2 (scip-python 0.6.8, scip-typescript 0.6.2) images built from
  deploy/sentinel; graphify installed hash-locked. The sandbox gained `collect` to return one result file.
  `sentinel record` ran both engines in the sandbox on all five fixtures (Python and TypeScript): all ok;
  outputs committed under tests/fixtures/sentinel/recorded with the fixture SHA.
- Mapping, schema probe, merge precedence, disagreement counts, ast fallback, tiers and node budget.
  Migration 0003 (eight sentinel_* tables). Export json, graphml, html (capped, says what it left out).

### M16 Cross-repository linking
- HTTP calls to endpoints, manifests (DEPENDS_ON with version check), Python imports and calls into other
  repositories, SCIP module references, topics, tables, data models, tests, Jira keys. Fixtures: all 12
  expected links found, no extra (precision 1.00, recall 1.00), all EXTRACTED with file:line evidence.

### M17 Enrichment
- Gateway provider `local_openai` (Ollama, llama.cpp, vLLM) with JSON-schema response_format; Sentinel
  aliases enrich, ask, embed_local; `llm: off` default; `sentinel doctor` health check. Through the
  gateway with the Fake provider (plumbing only): summaries, capability tags from the controlled list,
  community labels, embeddings in sentinel_embeddings; retries, budget stop and caching unit-tested.

### M18 Context API
- connectors/sentinel (port 8106): 10 read tools, registered `kind: read`. Live: tools listed, context
  pack returned evidence. Developer agent intake records a context pack (available in live runs).

### M19 Validation and evaluation
- `make sentinel-verify`: 7/7 validators passed on the fixture fleet:
  schema 286/286; integrity 0 dangling, 0 duplicates, 1.0% orphans; structural truth P 1.00 R 1.00, links
  P 1.00 R 1.00, graphify/SCIP agreement 0.78; evidence 164/164; review sample written (awaiting a
  human); question -> contract 48/50 (Fake keyword planner: plumbing only); key-fact coverage 0.90 vs grep
  0.31 vs pgvector search_code 0.53 (Fake embeddings), 492 vs 58 tokens per answer, ~50-95 ms.
- Synthetic 50-repository fleet (scripts/sentinel_perf.py, ast fallback): 51 repositories, 4,704 nodes,
  149 cross-repository edges; full build 8.9 s; one-file refresh 0.95 s (target < 60 s); query p50 45 ms,
  p95 83 ms (target < 2 s).
- Injection suite (unit): injected docstrings and READMEs stored as data and served `summary_untrusted`;
  hostile planner output with extra fields rejected; depth clamped; no secrets in graph or answers;
  commit messages only yield ticket keys; connector has no write tool.

### M20 Pilot
- `sentinel pilot` refuses with the example config (mock repositories, recorded runner) and lists what
  is missing; `--rehearsal` runs the flow on the fixtures and writes docs/sentinel/pilot-report.md,
  labelled as a rehearsal.

### Totals after Phase 2 (this machine, existing local data)
- `make check`: ruff clean, mypy --strict clean (72 files), 100 unit tests (63 Phase 0/1 + 37 Sentinel).
- `make verify` 6/6. `make coverage`: 137 passed (100 unit + 37 integration, 8 of them Sentinel) at 83.85% (floor 80%).
- `make dev-verify` 10/10 from a wiped stack (`make clean`, `make up`, `make dev-demo` with Developer eval
  1.0, then `make dev-verify`). On the earlier unwiped database it was 8/10 before Phase 2 and 4/10 after:
  adding the Sentinel read tools changed the Developer agent's config hash, so autonomy gating returned it
  to suggest_only until its eval passed again, and that eval could not pass on old data where every
  autofixable finding was already ticketed. A fresh stack removes the stale data and the eval re-unlocks it.
- After the wipe: `make demo` + `make verify` 6/6, `make sentinel-verify` 7/7 (rebuilt graph).

### Not verified
- Real repositories, real local model or Azure enrichment, GitHub REST discovery, the office pilot.
