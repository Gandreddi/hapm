# Sentinel (Phase 2 context agent)

Sentinel scans every onboarded repository, builds one knowledge graph that conforms to
`config/ontology.yaml`, links repositories to each other, and serves the result read-only to agents and
people. It never writes to a repository or to Jira. Decisions: ADR-0021 (engines), ADR-0022 (store,
linkers, context API), ADR-0023 (LLM modes, local models, Copilot rule).

## How a build works

```
GitHub connector (read tools) or fixture folder
  -> snapshot at a pinned SHA (excluded, secret-looking and oversized files dropped)
  -> facts (Sentinel's own parsers: endpoints, HTTP calls, topics, tables, manifests, imports)
  -> engines in the sandbox: graphify + SCIP  | graphify only | Python ast | failed
  -> merge (SCIP > graphify EXTRACTED > graphify INFERRED > AMBIGUOUS) and disagreement counts
  -> architecture edges (services, endpoints, topics, tables, tests, Jira keys)
  -> ontology conformance -> tiers and node budget -> Postgres (head SHA)
  -> enrichment through the gateway (only when llm is not off)
fleet: link layer across repositories (HTTP, manifests, imports and calls, SCIP modules)
```

## Commands

| Command | What it does |
|---|---|
| `uv run sentinel build [--repo X] [--force] [--dry-run] [--source local\|connector] [--runner recorded\|sandbox] [--temporal]` | Build incrementally (unchanged SHAs are skipped), then relink. `--force` ignores every cache (use after ontology, engine, model or embedding changes). `--dry-run` prints what would be rebuilt and the estimated LLM tokens. |
| `uv run sentinel link` | Rebuild only the cross-repository layer. |
| `uv run sentinel query '{"intent": "impact", "entities": ["get_stock"]}'` | Run one JSON contract (no model). |
| `uv run sentinel ask "What breaks if I change get_stock?"` | Question -> contract through the gateway `ask` alias. Disabled when llm is off. |
| `uv run sentinel export --format json\|graphml\|html --out FILE` | JSON and GraphML are complete; HTML shows at most `export.html_node_limit` nodes (tier 1 and best-connected first) and says how many it left out. |
| `uv run sentinel stats` / `sentinel diff REPO --from SHA --to SHA` | Counts, confidence mix, cross-repository edges; graph regression between SHAs. |
| `uv run sentinel verify [--review FILE]` | Validators 1-7. Writes docs/sentinel/verify-report.json and review-sample.yaml. |
| `uv run sentinel doctor` | Model health check (when llm is not off). |
| `uv run sentinel record --repo X` | Run the real engines in the sandbox and save their outputs for offline tests. |
| `uv run hapm repos discover --org ORG` | Write draft config/repos/<name>.yaml files with `sentinel.enabled: false`. |
| `uv run sentinel pilot --stage 1\|5\|all [--rehearsal]` | M20 pilot; refuses until the configuration is complete. |
| `make sentinel-engines` | Build the pinned engine images (graphify, SCIP). |
| `make sentinel-demo` / `make sentinel-verify` | Offline demo and the full validator run on the fixtures. |

## Query contract

```json
{"question": "...", "intent": "find|explain|path|impact|coverage", "entities": ["..."], "target": null,
 "repos": null, "relations": null, "depth": 2, "budget_tokens": 2000, "min_confidence": "INFERRED"}
```

Responses carry `answer`, `nodes`, `edges`, `evidence` (repo, sha, file, lines, confidence),
`path_confidence` (weakest hop), `truncated`, `dropped_evidence`, and a status: `answered`,
`ambiguous_entity` (with candidates; narrow with `repos`) or `not_found`.

## Validators (make sentinel-verify)

| # | Check | Target |
|---|---|---|
| 1 | Nodes and edges conform to the ontology | 100% |
| 2 | No dangling edges or duplicates; orphans | < 5% |
| 3 | Structural truth on fixtures (tests/fixtures/sentinel/truth.yaml); graphify/SCIP agreement on real repositories | P >= 0.95, R >= 0.90 |
| 4 | Every cited file and line exists at its SHA | 100% |
| 5 | Confidence mix; 10% sample of INFERRED and cross-repo edges for human review | measured P >= 0.85 |
| 6 | 50 golden questions -> contract (intent and entities exact) | >= 0.90 (real model only) |
| 7 | Key-fact coverage vs grep and the pgvector search_code baseline; tokens; latency | beats both |

## Ontology changes (migration notes)

Changing `config/ontology.yaml` must bump `version`, add a note here, and be followed by
`sentinel build --force` (the stored `ontology_version` makes incremental builds rebuild anyway).

- **Version 1 (2026-11-22)**: initial ontology from the Phase 2 prompt, plus node type `Ticket` (target of
  REFERENCES_TICKET) and relation `REFERENCES` (symbol use that is not a call or an import).

## Going live in the office

1. `make sentinel-engines` on the machine that runs the sandbox.
2. `uv run hapm repos discover --org <org>`; review drafts, set `jira_project`, then `sentinel.enabled: true`.
3. config/sentinel.yaml: `engines.runner: sandbox`, `service_aliases` for hosts that differ from repository
   names, the capability list. Keep `llm: off` for the first launch.
4. `uv run sentinel pilot --stage 1`, then `--stage 5`, then `--stage all`; read docs/sentinel/pilot-report.md.
5. Optional enrichment: start the local model server, add the `local-llm` provider and Sentinel aliases to
   config/providers.yaml, set `data_approval`, set `llm: local`, run `uv run sentinel doctor` until every
   check passes, then enable it repository by repository and compare validator 7 with and without it.
