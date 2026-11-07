# ADR-0008: Autonomy gated by evals on the exact config hash

- Status: accepted
- Date: 2026-11-07

## Context
Anyone can edit agent YAML, so a YAML value alone cannot enforce the rule that autonomy rises only after a passing eval.

## Decision
YAML holds the requested autonomy. At run start the orchestrator computes the config hash and looks up the latest offline eval for that agent and hash. Without a passing eval at or above the agent's threshold, the run is clamped to `suggest_only`, where write tools are recorded as suggestions and never executed.

## Options considered
Store the effective level in the database and change it through an API (stricter, but YAML stops being the full definition).

## Consequences
Any edit to an agent, even its prompt, resets it to suggest_only until its suite passes again. Tested in `tests/integration/test_m6_eval.py`.
