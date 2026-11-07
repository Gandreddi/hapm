"""Per-run and per-team token and cost budgets, counted in Redis."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from hapm_gateway.config import TeamBudget


@dataclass
class BudgetExceeded(Exception):
    scope: str
    metric: str
    limit: float
    used: float

    def reason(self) -> str:
        return f"{self.scope} {self.metric} budget exceeded: used {self.used:g} of {self.limit:g}"


class Counter(Protocol):
    async def get(self, key: str) -> float: ...

    async def add(self, key: str, amount: float, ttl_seconds: int) -> None: ...


class MemoryCounter:
    def __init__(self) -> None:
        self.values: dict[str, float] = {}

    async def get(self, key: str) -> float:
        return self.values.get(key, 0.0)

    async def add(self, key: str, amount: float, ttl_seconds: int) -> None:
        self.values[key] = self.values.get(key, 0.0) + amount


class RedisCounter:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url)

    async def get(self, key: str) -> float:
        value = await self._redis.get(key)
        return float(value) if value is not None else 0.0

    async def add(self, key: str, amount: float, ttl_seconds: int) -> None:
        pipe = self._redis.pipeline()
        pipe.incrbyfloat(key, amount)
        pipe.expire(key, ttl_seconds)
        await pipe.execute()

    async def ping(self) -> bool:
        return bool(await self._redis.ping())


RUN_TTL = 7 * 24 * 3600
TEAM_TTL = 2 * 24 * 3600


def _keys(team_id: str, run_id: str | None) -> dict[str, str]:
    day = datetime.now(UTC).strftime("%Y%m%d")
    keys = {"team_tokens": f"budget:team:{team_id}:{day}:tokens", "team_cost": f"budget:team:{team_id}:{day}:cost"}
    if run_id:
        keys["run_tokens"] = f"budget:run:{run_id}:tokens"
        keys["run_cost"] = f"budget:run:{run_id}:cost"
    return keys


class BudgetGuard:
    def __init__(self, counter: Counter) -> None:
        self.counter = counter

    async def check(self, budget: TeamBudget, team_id: str, run_id: str | None) -> None:
        """Refuse the call if any budget is already used up."""
        keys = _keys(team_id, run_id)
        checks = [
            ("team", "tokens", "team_tokens", budget.per_team_daily.tokens),
            ("team", "cost_usd", "team_cost", budget.per_team_daily.cost_usd),
        ]
        if run_id:
            checks += [
                ("run", "tokens", "run_tokens", budget.per_run.tokens),
                ("run", "cost_usd", "run_cost", budget.per_run.cost_usd),
            ]
        for scope, metric, key, limit in checks:
            used = await self.counter.get(keys[key])
            if used >= limit:
                raise BudgetExceeded(scope, metric, float(limit), used)

    async def charge(self, team_id: str, run_id: str | None, tokens: int, cost: float) -> None:
        keys = _keys(team_id, run_id)
        await self.counter.add(keys["team_tokens"], tokens, TEAM_TTL)
        await self.counter.add(keys["team_cost"], cost, TEAM_TTL)
        if run_id:
            await self.counter.add(keys["run_tokens"], tokens, RUN_TTL)
            await self.counter.add(keys["run_cost"], cost, RUN_TTL)
