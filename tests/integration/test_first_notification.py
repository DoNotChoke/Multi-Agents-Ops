import json
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
from opsteam_core.clock import FakeClock
from opsteam_core.events import InProcessPublisher
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_db.secrets import DbSecretStore
from opsteam_ingest.notify import opened_consumer
from opsteam_ingest.relay import relay_once
from opsteam_notify.throttle import Throttle
from opsteam_notify.webhook import DeliveryFailed, FakeNotifier, Message
from prometheus_client import REGISTRY
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import SECRETS_KEY
from tests.integration.test_incident_manager import T0, Shop, _NoLLM

pytestmark = pytest.mark.integration

REDIS_URL = f"redis://:{os.environ.get('REDIS_PASSWORD', 'dev-only-redis')}@127.0.0.1:6379/0"
SIGNING = "dev-only-signing-secret"
DURATIONS = (
    "time_to_detect_seconds_count",
    "time_to_detect_seconds_sum",
    "time_to_notify_seconds_count",
)
RAW_LOG = "Traceback: password=hunter2 at db.connect"


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


class Notice:
    """A shop tenant with webhook channels and the `incident.opened` consumer under test."""

    def __init__(self, shop: Shop, redis: Redis) -> None:
        self.shop, self.fake = shop, FakeNotifier()
        self.secrets = DbSecretStore(shop.engine, SECRETS_KEY)
        self.consume = opened_consumer(
            shop.engine, self.secrets, self.fake, Throttle(redis), FakeClock(T0),
            "http://app.example",
        )  # fmt: skip

    async def channel(self, status: str = "active", signed: bool = True) -> str:
        ref = await self.secrets.put(self.shop.ctx, SecretStr(SIGNING)) if signed else None
        channel_id = new_id("int")
        async with tenant_session(self.shop.engine, self.shop.ctx) as s:
            await s.execute(
                text("""
                    insert into integrations (integration_id, tenant_id, type, name, mode,
                                              status, endpoint, config, credentials_ref)
                    values (:id, :t, 'webhook', :id, 'direct', :status,
                            'https://hooks.example.com/x', '{}', :ref)
                """),
                {"id": channel_id, "t": self.shop.tenant, "status": status, "ref": ref},
            )
        return channel_id

    async def incident(self) -> str:
        await self.shop.process(await self.shop.event("checkout", extra={"log": RAW_LOG}))
        [incident] = await self.shop.incidents()
        return str(incident.incident_id)

    async def opened(self, incident_id: str) -> None:
        await self.consume(self.shop.ctx, {"incident_id": incident_id})

    async def state(self) -> tuple[str, Any]:
        [incident] = await self.shop.incidents()
        return incident.status, incident.first_notified_at

    def sent_to(self) -> list[str]:
        return [channel.channel_id for channel, _ in self.fake.sent]


async def test_a_new_incident_is_triaged_and_every_active_channel_told(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    notice = Notice(await Shop.create(app_engine), redis)
    channel = await notice.channel()
    await notice.channel(status="disabled")
    incident_id = await notice.incident()
    await notice.opened(incident_id)
    assert await notice.state() == ("TRIAGED", T0)
    [(sent_to, message)] = notice.fake.sent
    assert sent_to.channel_id == channel and sent_to.secret is not None
    assert sent_to.secret.get_secret_value() == SIGNING
    assert (message.severity, message.service, message.environment) == (
        "medium",
        "checkout",
        "shop",
    )
    assert message.started_at == T0 and message.url == f"http://app.example/incidents/{incident_id}"
    body = message.body().decode()
    assert "hunter2" not in body and "Traceback" not in body and SIGNING not in body
    [move] = await notice.shop.rows("select from_status, to_status from incident_transitions"
                                    " where to_status = 'TRIAGED'")  # fmt: skip
    assert tuple(move) == ("NEW", "TRIAGED")


async def test_a_failed_channel_is_retried_without_repeating_the_others(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    notice = Notice(await Shop.create(app_engine), redis)
    ok, down = await notice.channel(), await notice.channel(signed=False)
    incident_id = await notice.incident()
    notice.fake.failing.add(down)
    with pytest.raises(DeliveryFailed):
        await notice.opened(incident_id)
    assert notice.sent_to() == [ok]
    assert await notice.state() == ("TRIAGED", T0)
    notice.fake.failing.clear()
    await notice.opened(incident_id)
    await notice.opened(incident_id)  # a redelivery after success sends nothing
    assert sorted(notice.sent_to()) == sorted([ok, down])


async def test_no_channels_still_triages(app_engine: AsyncEngine, redis: Redis) -> None:
    notice = Notice(await Shop.create(app_engine), redis)
    await notice.opened(await notice.incident())
    assert await notice.state() == ("TRIAGED", None)


async def test_a_resolved_incident_is_not_announced(app_engine: AsyncEngine, redis: Redis) -> None:
    notice = Notice(await Shop.create(app_engine), redis)
    await notice.channel()
    incident_id = await notice.incident()
    async with tenant_session(app_engine, notice.shop.ctx) as s:
        await s.execute(text("update incidents set status = 'RESOLVED', resolved_at = now()"))
    await notice.opened(incident_id)
    assert notice.fake.sent == []


async def test_a_second_message_within_a_minute_is_held(redis: Redis) -> None:
    throttle, tenant = Throttle(redis), new_id("tnt")
    await throttle.sent(tenant, "int_A", "inc_1", "digest-1")
    assert not await throttle.allow(tenant, "int_A", "inc_1", "digest-2")
    assert await throttle.allow(tenant, "int_B", "inc_1", "digest-1")
    assert await throttle.allow(tenant, "int_A", "inc_2", "digest-3")
    assert not await throttle.allow(tenant, "int_A", "inc_2", "digest-1")


async def test_notification_goes_out_with_llm_forbidden(
    app_engine: AsyncEngine,
    system_engine: AsyncEngine,
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in [m for m in sys.modules if m.split(".")[0] in {"opsteam_llm", "opsteam_agents"}]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoLLM(), *sys.meta_path])
    notice = Notice(await Shop.create(app_engine), redis)
    await notice.channel()
    event_id = await notice.shop.event("checkout")
    async with tenant_session(app_engine, notice.shop.ctx) as s:
        await s.execute(
            text(
                "insert into outbox (tenant_id, topic, payload)"
                " values (:t, 'event.received', cast(:p as jsonb))"
            ),
            {"t": notice.shop.tenant, "p": json.dumps({"event_id": event_id})},
        )
    publisher = InProcessPublisher(
        {"event.received": notice.shop.consume, "incident.opened": notice.consume}
    )
    while await relay_once(system_engine, publisher):
        pass
    assert await notice.state() == ("TRIAGED", T0)
    assert len(notice.fake.sent) == 1 and isinstance(notice.fake.sent[0][1], Message)


async def test_detect_and_notify_times_are_measured_once(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    def sample(name: str) -> float:
        return REGISTRY.get_sample_value(name) or 0.0

    before = {n: sample(n) for n in DURATIONS}
    notice = Notice(await Shop.create(app_engine), redis)
    await notice.channel()
    incident_id = await notice.incident()
    await notice.opened(incident_id)
    await notice.opened(incident_id)  # a redelivery measures nothing again
    delta = {n: sample(n) - before[n] for n in DURATIONS}
    # The alert started at T0 and was received 30 s later.
    assert delta["time_to_detect_seconds_count"] == 1
    assert delta["time_to_detect_seconds_sum"] == 30
    assert delta["time_to_notify_seconds_count"] == 1
