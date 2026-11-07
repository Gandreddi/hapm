# ADR-0007: Approval gates pause the graph wherever policy requires

- Status: accepted
- Date: 2026-11-07

## Context
The spec lists execute, then gate, then finish, but the dummy agent's write must happen only after approval, and later agents need gates mid-run.

## Decision
Each tool step becomes two graph nodes: `check_<step>` asks policy, `act_<step>` calls LangGraph `interrupt()` when approval is needed. The activity returns the interrupt; the workflow creates the approval request, emits an event and waits on a `decide` signal with a timeout. Approve or edit-and-approve resumes the graph with `Command(resume=...)`; reject or timeout ends the run as rejected and skips all later steps. A run may have several gates.

## Options considered
One gate after a propose-only execute phase (simpler, but no read-after-write for later agents).

## Consequences
Splitting check and act means a resume never repeats the policy check or its audit row.
