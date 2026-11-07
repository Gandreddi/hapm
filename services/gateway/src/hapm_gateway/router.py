"""Route an alias to its targets in order, falling back on retryable errors and timeouts."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from hapm_gateway.config import GatewayConfig
from hapm_gateway.providers import Provider, ProviderError

T = TypeVar("T")


@dataclass
class Routed[T]:
    result: T
    provider: str
    model: str
    latency_ms: float
    attempts: list[str]


class Router:
    def __init__(self, config: GatewayConfig, providers: dict[str, Provider]) -> None:
        self.config = config
        self.providers = providers

    async def call(self, alias: str, fn: Callable[[Provider, str], Awaitable[T]]) -> Routed[T]:
        if alias not in self.config.aliases:
            raise ProviderError(400, f"unknown model alias {alias!r}", retryable=False)
        spec = self.config.aliases[alias]
        attempts: list[str] = []
        last: ProviderError | None = None
        for target in spec.targets:
            provider = self.providers.get(target.provider)
            if provider is None:
                attempts.append(f"{target.provider}:unavailable")
                last = ProviderError(503, f"provider {target.provider} is not configured", retryable=True)
                continue
            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(fn(provider, target.model), spec.timeout_seconds)
            except TimeoutError:
                attempts.append(f"{target.provider}:timeout")
                last = ProviderError(504, f"{target.provider} timed out", retryable=True)
                continue
            except ProviderError as exc:
                attempts.append(f"{target.provider}:{exc.status}")
                if not exc.retryable:
                    raise
                last = exc
                continue
            attempts.append(f"{target.provider}:ok")
            return Routed(result, target.provider, target.model, (time.perf_counter() - start) * 1000, attempts)
        assert last is not None
        raise ProviderError(last.status, f"all targets failed for {alias}: {', '.join(attempts)}", retryable=False)
