# ADR-0021: Sentinel extraction engines: graphify + SCIP, with the Python ast fallback

- Status: accepted
- Date: 2026-11-22

## Context
Sentinel (the Phase 2 context agent) must build a code graph for 50+ repositories in several languages,
with evidence for every edge and without sending code to a model. Engines must run without network or
credentials, and their output format must not change underneath us silently.

## Decision
- **graphify is the primary structural engine.** PyPI package `graphifyy` **0.9.115**, installed only
  hash-locked (`deploy/sentinel/graphify-requirements.txt`, generated with `uv pip compile --generate-hashes`).
  The README names **Graphify-Labs/graphify** as the official repository; licence Apache-2.0 / MIT. The
  hosted Graphify Cloud and its remote MCP endpoint are not used. Command, run with every LLM backend
  disabled (`GRAPHIFY_BACKEND=none` in the image): `graphify update . --force`, output
  `graphify-out/graph.json` (graph.schema_version 1). Verified against 0.9.115's `--help` on 2026-11-22.
- **SCIP adds compiler-precise definitions and references.** `scip` CLI **0.5.2** (release tarball
  checked against SHA-256 `49892eae…ab626`, recorded at pinning time), `@sourcegraph/scip-python`
  **0.6.8** and `@sourcegraph/scip-typescript` **0.6.2** (`deploy/sentinel/scip.Dockerfile`). scip-python
  runs with an empty `--environment` file so it never calls pip or the network.
- **The built-in `ast` extractor** (no dependency) is the fallback for Python.
- **Both engines run in the HAPM sandbox** (ADR-0017): the sandbox service gained an optional `collect`
  path that returns one result file from `/work` (bounded to 32 MB). Containers have no network, no
  credentials, dropped capabilities and CPU, memory and time limits; only the filtered snapshot (no
  excluded, secret-looking or oversized files) is sent. Images are allow-listed by exact tag.
- Fallback order per repository: graphify + SCIP -> graphify only (`precision_tier: approximate`) ->
  ast (`fallback`) -> failed with the reason. Precedence when engines agree on an edge: SCIP EXTRACTED >
  graphify EXTRACTED > graphify INFERRED > AMBIGUOUS. Disagreements are counted per run.
- A schema probe (`check_schema`) fails loudly when graph.schema_version, the pinned graphify_version or
  the field layout changes. Upgrades need a new entry here and a passing `make sentinel-verify`.
- `sentinel record` runs the real engines in the sandbox on the fixtures and commits their outputs under
  `tests/fixtures/sentinel/recorded`, with the fixture SHA in meta.json, so CI needs neither tool and a
  stale recording is detected.

## Options considered
tree-sitter directly (re-implements what graphify already does); Sourcegraph Enterprise (licence, separate
platform); a graph database (not needed at the measured scale, see ADR-0022).

## Consequences
- Observed while pinning: two pip installs hit SHA-256 mismatches on downloaded wheels and pip refused
  them; retries passed. Hash checking is doing its job, so keep it.
- SCIP cross-package symbols resolve only when the dependency is installed in the index environment.
  Without it, scip-python reports the imported module under the indexing project's own package and the
  symbol as local. Sentinel therefore matches modules across repositories by SCIP module descriptor and
  Python import facts, and marks version mismatches AMBIGUOUS (ADR-0022).
- SCIP records references, not calls; Sentinel counts a reference as CALLS only when the next character is
  `(`, otherwise REFERENCES. Functions nested inside functions are not separate nodes (as in ast).
- graphify gives start lines only; SCIP and ast supply full ranges when they ran.
