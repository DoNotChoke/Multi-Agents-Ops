"""Per-incident budget shared by the LLM and Tool Gateways: reserve before spending, then settle."""

import asyncio
from dataclasses import asdict, dataclass
from typing import Protocol

from redis.asyncio import Redis

from opsteam_core.errors import OpsteamError

# Counters expire well after the longest investigation, so they never leak.
TTL_S = 86400


class BudgetExceeded(OpsteamError):
    code = "budget.exceeded"


@dataclass(frozen=True)
class BudgetCaps:
    """Ceilings for one incident; cost is in micro-dollars so every counter is an integer."""

    llm_calls: int = 40
    tokens_in: int = 400_000
    tokens_out: int = 60_000
    tool_calls: int = 60
    cost_micro_usd: int = 5_000_000


def budget_key(tenant_id: str, scope: str) -> str:
    return f"t:{tenant_id}:budget:{scope}"


class Budget(Protocol):
    async def reserve(self, key: str, amounts: dict[str, int]) -> None:
        """Take all `amounts` at once, or none of them and raise BudgetExceeded."""
        ...

    async def settle(self, key: str, adjustments: dict[str, int]) -> None:
        """Correct a reservation to what was really used; negative gives back."""
        ...

    async def used(self, key: str) -> dict[str, int]: ...


class MemoryBudget:
    """In-process budget for tests and single-process tools."""

    def __init__(self, caps: BudgetCaps) -> None:
        self.caps = asdict(caps)
        self._used: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, key: str, amounts: dict[str, int]) -> None:
        async with self._lock:
            used = self._used.setdefault(key, {})
            if any(used.get(k, 0) + v > self.caps[k] for k, v in amounts.items()):
                raise BudgetExceeded("incident budget exhausted")
            for k, v in amounts.items():
                used[k] = used.get(k, 0) + v

    async def settle(self, key: str, adjustments: dict[str, int]) -> None:
        async with self._lock:
            used = self._used.setdefault(key, {})
            for k, v in adjustments.items():
                used[k] = used.get(k, 0) + v

    async def used(self, key: str) -> dict[str, int]:
        return dict(self._used.get(key, {}))


# ARGV: ttl, then (field, amount, cap) triples; all-or-nothing in one atomic script.
_RESERVE = """
for i = 2, #ARGV, 3 do
  local now = tonumber(redis.call('HGET', KEYS[1], ARGV[i]) or '0')
  if now + tonumber(ARGV[i + 1]) > tonumber(ARGV[i + 2]) then return 0 end
end
for i = 2, #ARGV, 3 do redis.call('HINCRBY', KEYS[1], ARGV[i], ARGV[i + 1]) end
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


class RedisBudget:
    """Budget shared across processes: every agent and gateway of one incident draws on it."""

    def __init__(self, redis: Redis, caps: BudgetCaps) -> None:
        self.redis, self.caps = redis, asdict(caps)
        self._reserve = redis.register_script(_RESERVE)

    async def reserve(self, key: str, amounts: dict[str, int]) -> None:
        args: list[str | int] = [TTL_S]
        for k, v in amounts.items():
            args += [k, v, self.caps[k]]
        if not await self._reserve(keys=[key], args=args):
            raise BudgetExceeded("incident budget exhausted")

    async def settle(self, key: str, adjustments: dict[str, int]) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            for k, v in adjustments.items():
                pipe.hincrby(key, k, v)
            pipe.expire(key, TTL_S)
            await pipe.execute()

    async def used(self, key: str) -> dict[str, int]:
        raw = await self.redis.hgetall(key)
        return {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}
