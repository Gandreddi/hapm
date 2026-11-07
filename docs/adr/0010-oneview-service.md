# ADR-0010: OneView backend as services/oneview

- Status: accepted
- Date: 2026-11-07

## Context
OneView-lite needs an event consumer, read tables, an API and a live stream; the original layout had no home for it.

## Decision
`services/oneview` consumes all topics into `oneview_events` (deduped by event id), serves runs, timelines, evals and costs over REST, and streams new events over server-sent events. The Next.js portal calls it server-side and proxies the stream so the browser never sees the bearer token.

## Options considered
Next.js route handlers with a Kafka client; folding it into the orchestrator.

## Consequences
A written deviation from the layout. The read model is eventually consistent, so tests poll with time limits.
