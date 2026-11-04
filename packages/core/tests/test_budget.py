import asyncio

import pytest
from opsteam_core.budget import BudgetCaps, BudgetExceeded, MemoryBudget


async def test_reserve_then_settle_to_what_was_actually_used() -> None:
    budget = MemoryBudget(BudgetCaps(tokens_out=100, llm_calls=5))
    key = "t:tnt_a:budget:inc_a"
    await budget.reserve(key, {"llm_calls": 1, "tokens_out": 80})
    await budget.settle(key, {"tokens_out": -50})  # used 30 of the 80 reserved
    await budget.reserve(key, {"llm_calls": 1, "tokens_out": 70})
    with pytest.raises(BudgetExceeded):
        await budget.reserve(key, {"llm_calls": 1, "tokens_out": 1})
    assert (await budget.used(key))["tokens_out"] == 100


async def test_a_refused_reservation_takes_nothing() -> None:
    budget = MemoryBudget(BudgetCaps(tool_calls=1, llm_calls=10))
    key = "t:tnt_a:budget:inc_b"
    with pytest.raises(BudgetExceeded):
        await budget.reserve(key, {"llm_calls": 1, "tool_calls": 2})
    assert (await budget.used(key)).get("llm_calls", 0) == 0


async def test_parallel_reservations_never_exceed_the_cap() -> None:
    budget = MemoryBudget(BudgetCaps(llm_calls=40))
    key = "t:tnt_a:budget:inc_c"

    async def one() -> bool:
        try:
            await budget.reserve(key, {"llm_calls": 1})
        except BudgetExceeded:
            return False
        return True

    results = await asyncio.gather(*(one() for _ in range(60)))
    assert sum(results) == 40
