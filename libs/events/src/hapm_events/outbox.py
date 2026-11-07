"""Transactional outbox: the event is written in the same transaction as the state change."""

import json
from typing import Any

from sqlalchemy import Connection, text

from hapm_events.envelope import Event, make_event, topic_for
from hapm_events.schemas import SchemaRegistry


def emit(
    conn: Connection,
    registry: SchemaRegistry,
    source: str,
    event_type: str,
    data: dict[str, Any],
    subject: str | None = None,
) -> Event:
    """Validate the event and insert it into the outbox using the caller's transaction."""
    registry.validate(event_type, data)
    event = make_event(source, event_type, data, subject)
    conn.execute(
        text("INSERT INTO outbox (id, topic, type, payload) " "VALUES (:id, :topic, :type, CAST(:payload AS jsonb))"),
        {
            "id": event.id,
            "topic": topic_for(event_type),
            "type": event_type,
            "payload": event.model_dump_json(),
        },
    )
    return event


def fetch_unpublished(conn: Connection, limit: int = 100) -> list[tuple[str, str, dict[str, Any]]]:
    """Lock a batch of unpublished rows so two relays never publish the same row at once."""
    rows = conn.execute(
        text(
            "SELECT id, topic, payload FROM outbox WHERE published_at IS NULL "
            "ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED"
        ),
        {"limit": limit},
    ).all()
    return [(str(r.id), r.topic, r.payload if isinstance(r.payload, dict) else json.loads(r.payload)) for r in rows]


def mark_published(conn: Connection, ids: list[str]) -> None:
    if ids:
        conn.execute(
            text("UPDATE outbox SET published_at = now() WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )


def already_processed(conn: Connection, consumer: str, event_id: str) -> bool:
    """Record the event for this consumer and return True if it had been seen before."""
    inserted = conn.execute(
        text(
            "INSERT INTO processed_events (consumer, event_id) VALUES (:c, :e) "
            "ON CONFLICT DO NOTHING RETURNING event_id"
        ),
        {"c": consumer, "e": event_id},
    ).first()
    return inserted is None
