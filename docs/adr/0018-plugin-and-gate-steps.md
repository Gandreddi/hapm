# ADR-0018: General plugin and gate steps in agent YAML

- Status: accepted
- Date: 2026-11-08

## Context
The Developer agent needs multi-stage logic and three human gates, but agents must remain configuration on the Core.

## Decision
Agent YAML gains two general step types: plugin (module:function, receives a PluginContext that only exposes Core clients: tools through the MCP client with policy and audit, LLM through the gateway, events) and gate (a plugin builds the approval request or returns None to skip; the graph interrupts exactly like tool approvals). A plugin can stop the run with status failed. Step ids plan and finish are reserved. In eval mode gates are recorded, not presented, and every write after them is dry-run by policy. Gate requests may name approvers from config/approvers.yaml, stored with the approval and checked by the API.

## Options considered
A separate Developer service with its own workflow (duplicates gates, audit and durability).

## Consequences
Future agents reuse the same mechanism. Plugins are trusted code reviewed like the Core.
