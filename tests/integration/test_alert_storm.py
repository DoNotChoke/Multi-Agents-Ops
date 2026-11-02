import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from opsteam_core.events import InProcessPublisher
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_ingest.relay import relay_once
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx
from tests.integration.test_first_notification import Notice
from tests.integration.test_incident_manager import Shop
from tests.integration.test_ingest_webhook import _alert, _alertmanager, _payload, _post, ingest

pytestmark = pytest.mark.integration

REDIS_URL = f"redis://:{os.environ.get('REDIS_PASSWORD', 'dev-only-redis')}@127.0.0.1:6379/0"
ALERTS = 500
PER_WEBHOOK = 50


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.mark.parametrize("collapse", [True, False], ids=["pods-collapse", "every-pod-distinct"])
async def test_a_storm_is_one_incident_and_one_notification(
    app_engine: AsyncEngine, system_engine: AsyncEngine, redis: Redis, collapse: bool
) -> None:
    tenant, iid, secret = await _alertmanager(app_engine, collapse_replica_labels=collapse)
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        await s.execute(
            text(
                "insert into services (service_id, tenant_id, name, environment)"
                " values (:i, :t, 'checkout', 'shop')"
            ),
            {"i": new_id("svc"), "t": tenant},
        )
    notice = Notice(Shop(app_engine, tenant, {}), redis)
    channel = await notice.channel()

    # Ten seconds of pods failing, as Alertmanager would deliver them in batches.
    begun = datetime.now(UTC)
    storm = [
        _alert(
            labels={
                "alertname": "HighErrorRate",
                "service": "checkout",
                "namespace": "shop",
                "severity": "page",
                "pod": f"checkout-{n}",
            },
            startsAt=(begun + timedelta(milliseconds=20 * n)).isoformat(),
        )
        for n in range(ALERTS)
    ]
    with ingest(app_engine, system_engine) as client:
        for start in range(0, ALERTS, PER_WEBHOOK):
            body = _payload(*storm[start : start + PER_WEBHOOK])
            resp = _post(client, iid, secret, json=body)
            assert resp.json() == {"accepted": PER_WEBHOOK, "duplicates": 0}, resp.text

    publisher = InProcessPublisher(
        {"event.received": notice.shop.consume, "incident.opened": notice.consume}
    )
    while await relay_once(system_engine, publisher):
        pass

    [incident] = await notice.shop.incidents()
    assert incident.events == ALERTS
    assert (incident.status, incident.severity) == ("TRIAGED", "high")
    assert notice.sent_to() == [channel]
    assert await notice.shop.rows("select 1 from outbox where published_at is null") == []
    assert await notice.shop.rows("select 1 from events where processed_at is null") == []
