# ADR-0022: Sentinel graph in Postgres, deterministic linkers and a read-only context API

- Status: accepted
- Date: 2026-11-22

## Context
Agents need cross-repository context (impact, paths, tests to run) with verifiable evidence. A model must
never write queries, and the context layer must never write to repositories or Jira.

## Decision
- **Storage: the existing Postgres + pgvector** (migration 0003): `sentinel_nodes`, `sentinel_edges`,
  `sentinel_runs`, `sentinel_heads`, `sentinel_files`, `sentinel_facts`, `sentinel_cache`,
  `sentinel_embeddings`. The current graph is each repository's head SHA; `retention_shas` older SHAs are
  kept for `sentinel diff`. Impact and path queries are recursive CTEs. No graph database: on a synthetic
  50-repository fleet (4,704 nodes) query p95 was 83 ms against a 2 s target.
- **Ontology** in `config/ontology.yaml` (version 1), loaded and enforced on every build; non-conforming
  nodes and edges are rejected and counted. `Ticket` is a node type (target of REFERENCES_TICKET) and
  `REFERENCES` covers symbol use that is neither a call nor an import.
- **Deterministic linkers** (no model): HTTP calls to endpoints (string constants resolve base URLs; hosts
  map to repositories through `service_aliases`, then `sentinel.service_names`, then name normalisation),
  package manifests (pyproject, package.json, go.mod, pom.xml), Python imports and calls into another
  repository's modules, SCIP module references, topics, tables, data models, tests and Jira keys in commit
  messages. A version mismatch or several candidates make an edge AMBIGUOUS; a match by name normalisation
  only is INFERRED.
- **JSON query contract** (`QueryContract`, extra fields forbidden, depth 1-4) executed by code. Every
  cited file and line is checked against `sentinel_files` and dropped if it does not exist; the response
  says how many were dropped. Model-written summaries are served with `summary_untrusted: true`.
- **connectors/sentinel** (MCP, port 8106) exposes only read tools: sentinel_query, sentinel_ask,
  sentinel_explain, sentinel_path, sentinel_impact, sentinel_neighbors, sentinel_find, sentinel_coverage,
  sentinel_stats and sentinel_context_pack. They are registered `kind: read` in connectors/tools.yaml, so
  agents' calls are policy-checked and audited like every other tool. Tests assert no write tool exists.
- **Fleet scheduling** is the `sentinel_fleet` Temporal workflow on the orchestrator task queue: one
  `sentinel_repo` child workflow per repository, bounded by `concurrency`, each build an activity with
  backoff retries; a retried build whose SHA is already stored returns `unchanged`.
- **Developer agent**: the intake step fetches `sentinel_context_pack` for the files in scope (best effort:
  Sentinel being down never blocks a scan) and records it in its output.

## Options considered
Neo4j or Memgraph (another service to run; not needed at measured latency); LLM-written Cypher or SQL
(unverifiable, injection-prone); vector search only (no structure or evidence).

## Consequences
- Rebuilding a repository removes its link-layer edges until the next link run; every build path relinks.
- Incremental work is per SHA (engine outputs) and per blob (enrichment). The engines themselves re-parse
  the whole repository for a new SHA; on the synthetic fleet a one-file refresh took 0.95 s.
