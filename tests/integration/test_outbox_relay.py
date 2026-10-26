import asyncio
import json
from typing import Any

import pytest
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_core.events import FakePublisher
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_ingest.relay import outbox_backlog, relay_once
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration


async def _queue(app_engine: AsyncEngine, count: int) -> tuple[str, list[str]]:
    """A new tenant with `count` unpublished outbox rows; return it and the event ids."""
    tenant, ids = new_id("tnt"), [new_id("evt") for _ in range(count)]
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        for event_id in ids:
            await s.execute(
                text(
                    "insert into outbox (tenant_id, topic, payload)"
                    " values (:t, 'event.received', cast(:p as jsonb))"
                ),
                {"t": tenant, "p": json.dumps({"event_id": event_id})},
            )
    return tenant, ids


async def _pending(app_engine: AsyncEngine, tenant: str) -> int:
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        count: int = await s.scalar(text("select count(*) from outbox where published_at is null"))
    return count


def _mine(fake: FakePublisher, tenant: str) -> list[str]:
    return [p["event_id"] for ctx, _, p in fake.published if ctx.tenant_id == tenant]


async def test_relay_publishes_in_order_under_each_rows_tenant(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, ids = await _queue(app_engine, 3)
    other, other_ids = await _queue(app_engine, 1)
    fake = FakePublisher()
    while await relay_once(system_engine, fake):
        pass
    assert _mine(fake, tenant) == ids and _mine(fake, other) == other_ids
    assert {topic for _, topic, _ in fake.published} == {"event.received"}
    assert await _pending(app_engine, tenant) == 0


async def test_two_relays_never_publish_the_same_row(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, ids = await _queue(app_engine, 20)

    class Slow(FakePublisher):
        async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None:
            await asyncio.sleep(0.01)  # hold the row locks while the other relay runs
            await super().publish(ctx, topic, payload)

    fake = Slow()
    while sum(await asyncio.gather(*(relay_once(system_engine, fake, 5) for _ in range(2)))):
        pass
    assert sorted(_mine(fake, tenant)) == sorted(ids)


async def test_a_crash_before_commit_republishes(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, [event_id] = await _queue(app_engine, 1)

    class Killed(FakePublisher):
        async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None:
            await super().publish(ctx, topic, payload)
            if ctx.tenant_id == tenant and len(_mine(self, tenant)) == 1:
                raise asyncio.CancelledError  # the process dies right after handing it over

    fake = Killed()
    with pytest.raises(asyncio.CancelledError):
        while await relay_once(system_engine, fake):
            pass
    assert await _pending(app_engine, tenant) == 1
    while await relay_once(system_engine, fake):
        pass
    # At least once: the consumer sees it again and must be idempotent.
    assert _mine(fake, tenant) == [event_id, event_id]
    assert await _pending(app_engine, tenant) == 0


async def test_a_failing_handler_keeps_the_row_and_the_order(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    tenant, ids = await _queue(app_engine, 3)

    class Flaky(FakePublisher):
        failures = 1

        async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None:
            if payload["event_id"] == ids[1] and self.failures:
                self.failures -= 1
                raise OpsteamError("consumer down")
            await super().publish(ctx, topic, payload)

    fake = Flaky()
    while fake.failures:
        await relay_once(system_engine, fake)
    # The failing call stopped at ids[1]; what it handed over before that is committed.
    assert _mine(fake, tenant) == ids[:1]
    assert await _pending(app_engine, tenant) == 2
    while await relay_once(system_engine, fake):
        pass
    assert _mine(fake, tenant) == ids


async def test_backlog_reports_count_and_age(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    await _queue(app_engine, 2)
    count, age = await outbox_backlog(system_engine)
    assert count >= 2 and age >= 0
    while await relay_once(system_engine, FakePublisher()):
        pass
    assert await outbox_backlog(system_engine) == (0, 0.0)
