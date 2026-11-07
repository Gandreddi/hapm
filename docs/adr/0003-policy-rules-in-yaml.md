# ADR-0003: Policy rules in YAML, default deny

- Status: accepted
- Date: 2026-11-07

## Context
Every LLM call, tool call and memory access must be checked against declarative rules before it runs, with allow, deny and require_approval outcomes.

## Decision
Rules live in `policies/default.yaml`, validated by Pydantic in `libs/policy`. Rules are checked top to bottom and the first match wins. A request that matches no rule is denied with rule id `default-deny`. Rules can match actor, action, resource and attributes with glob patterns. `require_approval` rules must name an approval policy (approvers, self-approval rule, timeout).

## Options considered
Rego with OPA: more expressive, but adds a service and a language the team must learn for Phase 0's small rule set.

## Consequences
Rules are easy to read and review. Complex conditions (time windows, data classes) will need either richer matchers or a move to Rego later; that move is logged in the backlog.
