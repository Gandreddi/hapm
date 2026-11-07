"""CloudEvents-style envelope."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TOPICS = {
    "run": "runs.events",
    "eval": "runs.events",
    "approval": "approvals.events",
    "audit": "audit.events",
    "llm": "llm.usage",
}
ALL_TOPICS = sorted(set(TOPICS.values()))


class Event(BaseModel):
    """Consumers ignore unknown fields, so newer producers never break older consumers."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    specversion: str = "1.0"
    schemaversion: int = 1
    source: str
    type: str
    subject: str | None = None
    time: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    data: dict[str, Any]


def topic_for(event_type: str) -> str:
    """Map an event type such as run.state_changed to its Kafka topic."""
    prefix = event_type.split(".", 1)[0]
    if prefix not in TOPICS:
        raise ValueError(f"no topic for event type {event_type!r}")
    return TOPICS[prefix]


def make_event(source: str, event_type: str, data: dict[str, Any], subject: str | None = None) -> Event:
    return Event(source=source, type=event_type, subject=subject, data=data)
