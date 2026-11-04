import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from opsteam_core.budget import BudgetCaps, BudgetExceeded, RedisBudget, budget_key
from opsteam_core.ids import new_id
from redis.asyncio import Redis

pytestmark = pytest.mark.integration

REDIS_URL = f"redis://:{os.environ.get('REDIS_PASSWORD', 'dev-only-redis')}@127.0.0.1:6379/0"


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


async def test_agents_in_parallel_never_overspend_one_incident(redis: Redis) -> None:
    budget = RedisBudget(redis, BudgetCaps(llm_calls=40, tool_calls=60))
    key = budget_key(new_id("tnt"), new_id("inc"))

    async def one() -> bool:
        try:
            await budget.reserve(key, {"llm_calls": 1, "tool_calls": 1})
        except BudgetExceeded:
            return False
        return True

    results = await asyncio.gather(*(one() for _ in range(100)))
    assert sum(results) == 40
    assert await budget.used(key) == {"llm_calls": 40, "tool_calls": 40}
    assert 0 < await redis.ttl(key) <= 86400


async def test_settling_gives_back_what_was_not_used(redis: Redis) -> None:
    budget = RedisBudget(redis, BudgetCaps(tokens_out=100))
    key = budget_key(new_id("tnt"), new_id("inc"))
    await budget.reserve(key, {"tokens_out": 90})
    with pytest.raises(BudgetExceeded):
        await budget.reserve(key, {"tokens_out": 20})
    await budget.settle(key, {"tokens_out": -60})
    await budget.reserve(key, {"tokens_out": 20})
    assert await budget.used(key) == {"tokens_out": 50}
