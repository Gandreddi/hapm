# ADR-0013: Pin the MCP Python SDK to 1.x

- Status: accepted
- Date: 2026-11-07

## Context
The MCP SDK released 2.x during Phase 0 with a renamed server API.

## Decision
Pin `mcp>=1.12,<2` (1.30 installed). Mock servers use FastMCP with stateless streamable HTTP, JSON responses and DNS-rebinding protection with explicit allowed hosts.

## Options considered
Adopt 2.x immediately (migration work with no Phase 0 benefit).

## Consequences
Upgrade to 2.x is in the backlog.
