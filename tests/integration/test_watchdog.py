import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_core.clock import FakeClock
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_incidents import lifecycle
from opsteam_incidents.lifecycle import Windows
from opsteam_incidents.watchdog import sweep
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


class Lab:
    """A tenant with one Alertmanager integration whose Watchdog repeats every minute."""

    def __init__(self, engine: AsyncEngine, tenant: str, integration_id: str) -> None:
        self.engine, self.tenant, self.integration_id = engine, tenant, integration_id
        self.ctx = system_ctx(tenant)

    @classmethod
    async def create(cls, engine: AsyncEngine, beat: datetime | None = T0) -> "Lab":
        tenant, integration_id = new_id("tnt"), new_id("int")
        async with tenant_session(engine, system_ctx(tenant)) as s:
            await s.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
            )
            await s.execute(
                text("""
                    insert into integrations (integration_id, tenant_id, type, name, mode,
                                              config, last_heartbeat_at)
                    values (:i, :t, 'alertmanager', 'lab-am', 'direct', cast(:c as jsonb), :b)
                """),
                {
                    "i": integration_id,
                    "t": tenant,
                    "c": json.dumps({"watchdog_interval_seconds": 60}),
                    "b": beat,
                },
            )
        return cls(engine, tenant, integration_id)

    async def sweep(self, system_engine: AsyncEngine, at: datetime) -> None:
        await sweep(self.engine, system_engine, FakeClock(at))

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with tenant_session(self.engine, self.ctx) as s:
            return list((await s.execute(text(sql), params)).all())

    async def incidents(self) -> list[Any]:
        return await self.rows("select * from incidents order by opened_at")

    async def set(self, sql: str, **params: Any) -> None:
        async with tenant_session(self.engine, self.ctx) as s:
            await s.execute(text(sql), params)


async def test_three_missed_heartbeats_open_one_visibility_lost_incident(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab = await Lab.create(app_engine)
    await lab.sweep(system_engine, T0 + timedelta(minutes=3))
    assert await lab.incidents() == []
    await lab.sweep(system_engine, T0 + timedelta(minutes=3, seconds=1))
    await lab.sweep(system_engine, T0 + timedelta(minutes=10))
    [incident] = await lab.incidents()
    assert (incident.source, incident.severity, incident.status) == ("watchdog", "high", "NEW")
    assert incident.service_id is None
    assert incident.unmapped_service == {"name": "platform:lab-am"}
    assert "lab-am" in incident.title
    [opened] = await lab.rows("select payload from outbox where topic = 'incident.opened'")
    assert opened.payload == {"incident_id": incident.incident_id}
    # It has no alerts, so the lifecycle sweep must leave it to the watchdog.
    await lifecycle.sweep(app_engine, system_engine, FakeClock(T0 + timedelta(hours=2)), Windows())
    assert [i.status for i in await lab.incidents()] == ["NEW"]


async def test_a_returning_heartbeat_resolves_it(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab = await Lab.create(app_engine)
    await lab.sweep(system_engine, T0 + timedelta(minutes=5))
    await lab.set("update integrations set last_heartbeat_at = :b", b=T0 + timedelta(minutes=6))
    await lab.sweep(system_engine, T0 + timedelta(minutes=6, seconds=30))
    [incident] = await lab.incidents()
    assert incident.status == "RESOLVED"
    [move] = await lab.rows("select reason from incident_transitions where to_status = 'RESOLVED'")
    assert move.reason == "visibility_restored"
    # Losing it again later opens a fresh incident.
    await lab.sweep(system_engine, T0 + timedelta(minutes=20))
    assert [i.status for i in await lab.incidents()] == ["RESOLVED", "NEW"]


async def test_no_watchdog_ever_seen_means_no_alarm(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab = await Lab.create(app_engine, beat=None)
    await lab.sweep(system_engine, T0 + timedelta(days=1))
    assert await lab.incidents() == []


async def test_disabling_the_integration_resolves_and_silences_it(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab = await Lab.create(app_engine)
    await lab.sweep(system_engine, T0 + timedelta(minutes=5))
    await lab.set("update integrations set status = 'disabled'")
    await lab.sweep(system_engine, T0 + timedelta(minutes=6))
    await lab.sweep(system_engine, T0 + timedelta(hours=1))
    [incident] = await lab.incidents()
    assert incident.status == "RESOLVED"


async def test_the_interval_comes_from_the_integration(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    lab = await Lab.create(app_engine)
    await lab.set("update integrations set config = '{}'")  # default: 5 minutes
    await lab.sweep(system_engine, T0 + timedelta(minutes=14))
    assert await lab.incidents() == []
    await lab.sweep(system_engine, T0 + timedelta(minutes=15, seconds=1))
    assert len(await lab.incidents()) == 1
