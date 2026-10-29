from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_core.clock import FakeClock
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_incidents import watchdog
from opsteam_ingest import intake
from opsteam_ingest.alertmanager import Alert
from opsteam_ingest.reconcile import reconcile_once
from opsteam_obs.metrics import RECONCILIATION_GAP
from opsteam_tools.connectors.http import ConnectorError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

STARTS = "2026-10-29T09:00:00Z"


def _alert(service: str = "checkout", alertname: str = "HighErrorRate") -> dict[str, Any]:
    """An alert as Alertmanager's API v2 lists it."""
    return {
        "labels": {"alertname": alertname, "service": service, "namespace": "shop"},
        "annotations": {"summary": f"{service} returns 5xx"},
        "startsAt": STARTS,
        "endsAt": "2026-10-29T09:30:00Z",
        "generatorURL": "http://prometheus:9090/graph",
        "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
    }


class FakeAlertmanagers:
    """Answers per endpoint with a list of alerts, or raises ConnectorError when down."""

    def __init__(self) -> None:
        self.answers: dict[str, list[dict[str, Any]] | None] = {}
        self.calls: list[str] = []

    async def __call__(self, endpoint: str) -> list[dict[str, Any]]:
        self.calls.append(endpoint)
        answer = self.answers.get(endpoint)
        if answer is None:
            raise ConnectorError("alertmanager could not be reached")
        return answer


class Tenant:
    def __init__(self, engine: AsyncEngine, tenant: str, integration_id: str, endpoint: str):
        self.engine, self.tenant, self.integration_id = engine, tenant, integration_id
        self.endpoint, self.ctx = endpoint, system_ctx(tenant)

    @classmethod
    async def create(
        cls, engine: AsyncEngine, endpoint: str | None = None, status: str = "active"
    ) -> "Tenant":
        tenant, integration_id = new_id("tnt"), new_id("int")
        endpoint = endpoint or f"https://{tenant.lower()}.example.com:9093"
        async with tenant_session(engine, system_ctx(tenant)) as s:
            await s.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
            )
            await s.execute(
                text("""
                    insert into integrations (integration_id, tenant_id, type, name, mode,
                                              status, endpoint, config)
                    values (:i, :t, 'alertmanager', 'am', 'direct', :s, :e, '{}')
                """),
                {"i": integration_id, "t": tenant, "s": status, "e": endpoint},
            )
        return cls(engine, tenant, integration_id, endpoint)

    async def rows(self, sql: str) -> list[Any]:
        async with tenant_session(self.engine, self.ctx) as s:
            return list((await s.execute(text(sql))).all())

    async def failures(self) -> int:
        [(count,)] = await self.rows("select reconcile_failures from integrations")
        return int(count)


async def test_a_missing_alert_is_filled_in_once(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab, am = await Tenant.create(app_engine), FakeAlertmanagers()
    am.answers[lab.endpoint] = [_alert("checkout"), _alert("payment")]
    # checkout already arrived by webhook; payment's webhook was lost.
    webhook = Alert.model_validate(_alert("checkout") | {"status": "firing", "endsAt": None})
    await intake.store(app_engine, lab.ctx, lab.integration_id, {}, [webhook])
    before = RECONCILIATION_GAP._value.get()
    await reconcile_once(app_engine, system_engine, am)
    await reconcile_once(app_engine, system_engine, am)
    assert RECONCILIATION_GAP._value.get() - before == 1
    events = await lab.rows("select labels, annotations, status, ends_at from events")
    assert len(events) == 2
    [filled] = [e for e in events if e.labels["service"] == "payment"]
    assert filled.annotations["reconciled"] == "true"
    assert "reconciled" not in filled.labels  # labels feed the fingerprint
    assert (filled.status, filled.ends_at) == ("firing", None)
    outbox = await lab.rows("select topic from outbox")
    assert [o.topic for o in outbox] == ["event.received", "event.received"]
    assert await lab.failures() == 0


async def test_the_watchdog_is_not_a_heartbeat_when_read_back(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab, am = await Tenant.create(app_engine), FakeAlertmanagers()
    am.answers[lab.endpoint] = [_alert("meta", alertname="Watchdog")]
    await reconcile_once(app_engine, system_engine, am)
    assert await lab.rows("select 1 from events") == []
    [(beat,)] = await lab.rows("select last_heartbeat_at from integrations")
    assert beat is None


async def test_unreadable_three_times_in_a_row_means_visibility_lost(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab, am = await Tenant.create(app_engine), FakeAlertmanagers()
    now = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    for _ in range(2):
        await reconcile_once(app_engine, system_engine, am)
    await watchdog.sweep(app_engine, system_engine, FakeClock(now))
    assert await lab.rows("select 1 from incidents") == []
    await reconcile_once(app_engine, system_engine, am)
    assert await lab.failures() == 3
    await watchdog.sweep(app_engine, system_engine, FakeClock(now))
    [incident] = await lab.rows("select title, status, source from incidents")
    assert incident.title == "Visibility lost: cannot read alerts from am"
    assert (incident.status, incident.source) == ("NEW", "watchdog")
    # One good read clears it.
    am.answers[lab.endpoint] = []
    await reconcile_once(app_engine, system_engine, am)
    await watchdog.sweep(app_engine, system_engine, FakeClock(now + timedelta(seconds=30)))
    [incident] = await lab.rows("select status from incidents")
    assert incident.status == "RESOLVED"


async def test_only_active_integrations_with_an_endpoint_are_read(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    disabled = await Tenant.create(app_engine, status="disabled")
    am = FakeAlertmanagers()
    await reconcile_once(app_engine, system_engine, am)
    assert disabled.endpoint not in am.calls
    assert await disabled.failures() == 0


async def test_malformed_alerts_are_skipped_not_fatal(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab, am = await Tenant.create(app_engine), FakeAlertmanagers()
    am.answers[lab.endpoint] = [{"labels": "nope"}, _alert("payment")]
    await reconcile_once(app_engine, system_engine, am)
    assert len(await lab.rows("select 1 from events")) == 1
    assert await lab.failures() == 0
