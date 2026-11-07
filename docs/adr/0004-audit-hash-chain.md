# ADR-0004: Append-only audit log with one global hash chain

- Status: accepted
- Date: 2026-11-07

## Context
Audit rows must be tamper-evident and the application must not be able to change them.

## Decision
`audit_log` stores each row's hash over its own fields plus the previous row's hash. Writes take a transaction-level Postgres advisory lock so the chain never forks. The `hapm_app` role has SELECT and INSERT only; UPDATE, DELETE and TRUNCATE are revoked. Full prompts, responses and tool payloads go to Blob storage keyed by content hash; rows hold only the hashes. `GET /v1/audit/verify` and `hapm audit verify` walk the whole chain.

## Options considered
Per-run chains (parallel writes, but deleting a whole run's rows goes unnoticed); signing rows (needs key management).

## Consequences
Audit writes are serialised, which is fine at Phase 0 volumes. Someone with database owner rights could rewrite the whole chain consistently; external anchoring of chain checkpoints is in the backlog.
