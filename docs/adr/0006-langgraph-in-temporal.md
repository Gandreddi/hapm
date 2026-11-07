# ADR-0006: LangGraph runs inside Temporal activities

- Status: accepted
- Date: 2026-11-07

## Context
Runs must be durable (survive a worker crash) and the agent graph should use LangGraph. Temporal workflow code must be deterministic, which LangGraph is not.

## Decision
The `agent_run` workflow owns the overall flow, gates and the terminal state, and does no I/O. LangGraph runs inside the `run_graph` activity with a Postgres checkpointer keyed by run_id, so a retried activity resumes from the last completed node. Activities heartbeat after each node (15 second heartbeat timeout), so a killed worker's activity is retried within seconds.

## Options considered
Each LangGraph node as its own activity (bypasses LangGraph's runtime); LangGraph without Temporal (no durable timers or signals).

## Consequences
Write tools need idempotency keys because a node may run twice after a crash. Proven by the worker kill test in `make verify` (f).
