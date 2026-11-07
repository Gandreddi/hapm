# ADR-0009: Eval mode dry-runs write tools

- Status: accepted
- Date: 2026-11-07

## Context
Evals run the real workflow but cannot wait for a person, and approvals must never be automatic.

## Decision
Tasks submitted with `mode: eval` (admin only) run normally except that the policy rule `tools-write-eval-dry-run` allows write tools in dry-run form: the call is validated, recorded and audited but not executed. Expected tool calls are scored from that record.

## Options considered
A dedicated automated approver (adds an approver nobody watches).

## Consequences
Eval runs never create stories. Online evaluation scores live runs separately and skips eval runs.
