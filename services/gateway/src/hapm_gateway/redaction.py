"""Redaction hook applied to prompts and responses before they are stored or logged."""

import re
from collections.abc import Callable

from hapm_gateway.config import Redaction

RedactHook = Callable[[str], str]


class Redactor:
    def __init__(self, config: Redaction, extra_hooks: list[RedactHook] | None = None) -> None:
        self.enabled = config.enabled
        self._patterns = [(name, re.compile(p)) for name, p in config.patterns.items()]
        self._hooks = extra_hooks or []

    def __call__(self, text: str) -> str:
        if not self.enabled:
            return text
        for name, pattern in self._patterns:
            text = pattern.sub(f"[REDACTED:{name}]", text)
        for hook in self._hooks:
            text = hook(text)
        return text
