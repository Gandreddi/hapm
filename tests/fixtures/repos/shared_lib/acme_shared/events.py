"""Tiny event bus facade. Real deployments publish to Kafka topics."""

from typing import Any

_SENT: list[tuple[str, dict[str, Any]]] = []


def publish(topic: str, payload: dict[str, Any]) -> None:
    """Publish one event to a topic."""
    _SENT.append((topic, payload))


def subscribe(topic: str):  # type: ignore[no-untyped-def]
    """Decorator that registers a handler for a topic."""

    def register(handler):  # type: ignore[no-untyped-def]
        handler.topic = topic
        return handler

    return register
