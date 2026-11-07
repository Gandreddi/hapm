# ADR-0005: CloudEvents-style envelope and transactional outbox

- Status: accepted
- Date: 2026-11-07

## Context
State changes and their events must never disagree, and Kafka can be briefly unavailable.

## Decision
Every event uses one envelope (id, specversion, schemaversion, source, type, subject, time, data) with one JSON Schema per type in `schemas/events`. Producers validate before writing. Services insert the event into the `outbox` table in the same transaction as the state change. `services/relay` (its own process, a written deviation from the layout) publishes committed rows to Kafka and marks them published. Delivery is at-least-once; consumers dedupe by event id and ignore unknown fields.

## Options considered
Publishing directly to Kafka from services (lost or phantom events on failure); Debezium change data capture (another moving part).

## Consequences
A relay crash between publish and mark causes a duplicate, which consumers absorb. The relay is a single instance in Phase 0; `FOR UPDATE SKIP LOCKED` already allows more.
