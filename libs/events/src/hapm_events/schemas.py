"""One JSON Schema per event type, loaded from the schemas/events directory."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class SchemaValidationError(ValueError):
    pass


class SchemaRegistry:
    def __init__(self, directory: Path) -> None:
        self._validators: dict[str, Draft202012Validator] = {}
        for path in sorted(directory.glob("*.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self._validators[path.stem] = Draft202012Validator(schema)
        if not self._validators:
            raise ValueError(f"no event schemas found in {directory}")

    @property
    def types(self) -> list[str]:
        return sorted(self._validators)

    def validate(self, event_type: str, data: dict[str, Any]) -> None:
        validator = self._validators.get(event_type)
        if validator is None:
            raise SchemaValidationError(f"unknown event type {event_type!r}")
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if errors:
            raise SchemaValidationError(f"{event_type}: {errors[0].message}")
