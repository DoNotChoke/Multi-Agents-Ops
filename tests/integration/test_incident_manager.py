import asyncio
import importlib.abc
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_core.clock import FakeClock
from opsteam_core.events import InProcessPublisher
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_incidents.fingerprint import fingerprint
from opsteam_incidents.lifecycle import Windows, sweep
from opsteam_incidents.manager import event_consumer
from opsteam_ingest.relay import relay_once
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


class Shop:
    """A tenant with frontend -> checkout -> payment (arrows point at what a service calls)."""

    def __init__(self, engine: AsyncEngine, tenant: str, services: dict[str, str]) -> None:
        self.engine, self.tenant, self.services = engine, tenant, services
        self.ctx = system_ctx(tenant)
        self.consume = event_consumer(engine)

    @classmethod
    async def create(cls, engine: AsyncEngine, environment: str = "shop") -> "Shop":
        tenant = new_id("tnt")
        services = {name: new_id("svc") for name in ("frontend", "checkout", "payment", "auth")}
        async with tenant_session(engine, system_ctx(tenant)) as s:
            await s.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
            )
            for name, service_id in services.items():
                await s.execute(
                    text(
                        "insert into services (service_id, tenant_id, name, environment,"
                        " criticality) values (:i, :t, :n, :e, 'high')"
                    ),
                    {"i": service_id, "t": tenant, "n": name, "e": environment},
                )
            for down, up in (("frontend", "checkout"), ("checkout", "payment")):
                await s.execute(
                    text(
                        "insert into service_dependencies (tenant_id, service_id,"
                        " depends_on_service_id) values (:t, :d, :u)"
                    ),
                    {"t": tenant, "d": services[down], "u": services[up]},
                )
        return cls(engine, tenant, services)

    async def event(
        self,
        service: str | None,
        alertname: str = "HighLatencyP95",
        *,
        status: str = "firing",
        severity: str = "warning",
        starts: datetime = T0,
        received: datetime | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Store an event the way intake does and return its id."""
        labels = {"alertname": alertname, "namespace": "shop"} | (extra or {})
        if service:
            labels["service"] = service
        event_id = new_id("evt")
        async with tenant_session(self.engine, self.ctx) as s:
            await s.execute(
                text("""
                    insert into events (event_id, tenant_id, source_type, fingerprint, status,
                                        severity_hint, title, labels, annotations, starts_at,
                                        received_at)
                    values (:id, :t, 'alertmanager', :fp, :status, :sev, :title,
                            cast(:labels as jsonb), '{}', :starts, :received)
                """),
                {
                    "id": event_id,
                    "t": self.tenant,
                    "fp": fingerprint(
                        self.tenant, "", "alertmanager", alertname, labels, collapse=True
                    ),
                    "status": status,
                    "sev": severity,
                    "title": f"{alertname} on {service}",
                    "labels": json.dumps(labels),
                    "starts": starts,
                    "received": received or starts + timedelta(seconds=30),
                },
            )
        return event_id

    async def process(self, event_id: str) -> None:
        await self.consume(self.ctx, {"event_id": event_id})

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with tenant_session(self.engine, self.ctx) as s:
            return list((await s.execute(text(sql), params)).all())

    async def incidents(self) -> list[Any]:
        return await self.rows(
            "select i.*, (select count(*) from incident_events ie"
            " where ie.incident_id = i.incident_id) as events from incidents i order by opened_at"
        )


async def test_first_alert_opens_an_incident(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    event_id = await shop.event("checkout")
    await shop.process(event_id)
    [incident] = await shop.incidents()
    assert (incident.status, incident.severity, incident.events) == ("NEW", "medium", 1)
    assert incident.service_id == shop.services["checkout"]
    assert incident.unmapped_service is None
    assert incident.source == "alert" and incident.title == "HighLatencyP95 on checkout"
    assert incident.detected_at == T0 + timedelta(seconds=30)
    [event] = await shop.rows("select service_ref, processed_at from events")
    assert event.service_ref["service_id"] == shop.services["checkout"]
    assert event.processed_at is not None
    [history] = await shop.rows("select from_status, to_status from incident_transitions")
    assert tuple(history) == (None, "NEW")
    [outbox] = await shop.rows("select topic, payload from outbox")
    assert (outbox.topic, outbox.payload) == (
        "incident.opened",
        {"incident_id": incident.incident_id},
    )
    actions = [r.action for r in await shop.rows("select action from audit_events order by seq")]
    assert actions == ["incident.opened"]


async def test_redelivery_changes_nothing(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    event_id = await shop.event("checkout")
    for _ in range(3):
        await shop.process(event_id)
    [incident] = await shop.incidents()
    assert incident.events == 1
    assert len(await shop.rows("select 1 from outbox")) == 1


async def test_rule_1_same_fingerprint_joins(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    # Alertmanager re-fires the same alert later with a new start time.
    await shop.process(await shop.event("checkout", starts=T0 + timedelta(hours=2)))
    [incident] = await shop.incidents()
    assert incident.events == 2


async def test_rule_2_same_service_within_the_window_joins(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(
        await shop.event("checkout", "HighErrorRate", starts=T0 + timedelta(minutes=10))
    )
    late = await shop.event("checkout", "PodRestarting", starts=T0 + timedelta(minutes=20))
    await shop.process(late)
    incidents = await shop.incidents()
    assert [i.events for i in incidents] == [2, 1]  # 20 minutes is outside the window


async def test_rule_3_an_upstream_failure_joins_and_becomes_the_root(
    app_engine: AsyncEngine,
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    # payment is what checkout calls: it starts failing within 5 minutes.
    await shop.process(
        await shop.event("payment", "HighErrorRate", starts=T0 + timedelta(minutes=2))
    )
    # frontend calls checkout: downstream, joins without moving the root.
    await shop.process(
        await shop.event("frontend", "HighErrorRate", starts=T0 + timedelta(minutes=3))
    )
    [incident] = await shop.incidents()
    assert incident.events == 3
    assert incident.service_id == shop.services["payment"]
    rules = [
        r.resource["rule"]
        for r in await shop.rows(
            "select resource from audit_events"
            " where action = 'incident.event_attached' order by seq"
        )
    ]
    assert rules == ["dependency", "dependency"]


async def test_rule_3_needs_close_start_times_and_a_real_dependency(
    app_engine: AsyncEngine,
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(
        await shop.event("payment", "HighErrorRate", starts=T0 + timedelta(minutes=8))
    )
    await shop.process(await shop.event("auth", "HighErrorRate", starts=T0 + timedelta(minutes=1)))
    assert [i.events for i in await shop.incidents()] == [1, 1, 1]


async def test_unmapped_services_still_open_incidents(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("cart"))
    await shop.process(await shop.event(None, "NodeDown"))
    unmapped, no_name = await shop.incidents()
    assert unmapped.service_id is None
    assert unmapped.unmapped_service == {"name": "cart", "environment": "shop"}
    assert no_name.unmapped_service == {"environment": "shop"} and no_name.service_id is None


async def test_severity_follows_the_worst_alert_and_the_blast_radius(
    app_engine: AsyncEngine,
) -> None:
    shop = await Shop.create(app_engine, environment="production")
    prod = {"environment": "production"}
    await shop.process(await shop.event("checkout", extra=prod))
    # production + high criticality: warning -> high
    assert (await shop.incidents())[0].severity == "high"
    await shop.process(
        await shop.event(
            "checkout",
            "HighErrorRate",
            severity="critical",
            starts=T0 + timedelta(minutes=1),
            extra=prod,
        )
    )
    assert (await shop.incidents())[0].severity == "critical"


async def test_three_services_raise_severity(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(
        await shop.event("payment", "HighErrorRate", starts=T0 + timedelta(minutes=1))
    )
    assert (await shop.incidents())[0].severity == "medium"
    await shop.process(
        await shop.event("frontend", "HighErrorRate", starts=T0 + timedelta(minutes=2))
    )
    assert (await shop.incidents())[0].severity == "high"


async def test_a_resolution_attaches_without_changing_state(app_engine: AsyncEngine) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(await shop.event("checkout", status="resolved"))
    await shop.process(await shop.event("cart", status="resolved"))  # nothing to attach to
    [incident] = await shop.incidents()
    assert (incident.status, incident.events) == ("NEW", 2)


class _NoLLM(importlib.abc.MetaPathFinder):
    def find_spec(self, name: str, *_: Any) -> None:
        if name.split(".")[0] in {"opsteam_llm", "opsteam_agents"}:
            raise ImportError(f"T0 must not import {name}")


async def test_detection_runs_with_llm_forbidden(
    app_engine: AsyncEngine, system_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [m for m in sys.modules if m.split(".")[0] in {"opsteam_llm", "opsteam_agents"}]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoLLM(), *sys.meta_path])
    shop = await Shop.create(app_engine)
    event_id = await shop.event("checkout")
    async with tenant_session(app_engine, shop.ctx) as s:
        await s.execute(
            text(
                "insert into outbox (tenant_id, topic, payload)"
                " values (:t, 'event.received', cast(:p as jsonb))"
            ),
            {"t": shop.tenant, "p": json.dumps({"event_id": event_id})},
        )

    async def ignore(*_: Any) -> None:
        return None

    publisher = InProcessPublisher({"event.received": shop.consume, "incident.opened": ignore})
    while await relay_once(system_engine, publisher):
        pass
    assert len(await shop.incidents()) == 1


async def _sweep(shop: Shop, system_engine: AsyncEngine, at: datetime) -> None:
    await sweep(shop.engine, system_engine, FakeClock(at), Windows())


async def test_auto_resolve_waits_for_every_alert_to_stay_quiet(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(
        await shop.event("checkout", "HighErrorRate", starts=T0 + timedelta(minutes=1))
    )
    quiet = T0 + timedelta(minutes=5)
    await shop.process(await shop.event("checkout", status="resolved", received=quiet))
    await _sweep(shop, system_engine, quiet + timedelta(hours=1))
    assert (await shop.incidents())[0].status == "NEW"  # HighErrorRate still firing
    await shop.process(
        await shop.event(
            "checkout",
            "HighErrorRate",
            status="resolved",
            starts=T0 + timedelta(minutes=1),
            received=quiet,
        )
    )
    await _sweep(shop, system_engine, quiet + timedelta(minutes=9, seconds=59))
    assert (await shop.incidents())[0].status == "NEW"
    await _sweep(shop, system_engine, quiet + timedelta(minutes=10))
    [incident] = await shop.incidents()
    assert (incident.status, incident.resolved_at) == ("RESOLVED", quiet + timedelta(minutes=10))
    [why] = await shop.rows("select reason from incident_transitions where to_status = 'RESOLVED'")
    assert why.reason == "auto_resolved"


async def test_a_resolved_incident_closes_after_a_day(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(await shop.event("checkout", status="resolved", received=T0))
    resolved = T0 + timedelta(minutes=10)
    await _sweep(shop, system_engine, resolved)
    await _sweep(shop, system_engine, resolved + timedelta(hours=23, minutes=59))
    assert (await shop.incidents())[0].status == "RESOLVED"
    await _sweep(shop, system_engine, resolved + timedelta(hours=24))
    assert (await shop.incidents())[0].status == "CLOSED"


async def test_a_refire_soon_after_resolving_reopens_the_same_incident(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(await shop.event("checkout", status="resolved", received=T0))
    resolved = T0 + timedelta(minutes=10)
    await _sweep(shop, system_engine, resolved)
    refire = resolved + timedelta(minutes=29)
    await shop.process(await shop.event("checkout", starts=refire, received=refire))
    [incident] = await shop.incidents()
    assert (incident.status, incident.events, incident.resolved_at) == ("INVESTIGATING", 3, None)
    [why] = await shop.rows(
        "select reason from incident_transitions where to_status = 'INVESTIGATING'"
    )
    assert why.reason.startswith("reopened")


async def test_a_late_refire_opens_a_new_incident(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(await shop.event("checkout", status="resolved", received=T0))
    resolved = T0 + timedelta(minutes=10)
    await _sweep(shop, system_engine, resolved)
    late = resolved + timedelta(minutes=31)
    await shop.process(await shop.event("checkout", starts=late, received=late))
    assert [i.status for i in await shop.incidents()] == ["RESOLVED", "NEW"]


async def test_two_sweeps_at_once_resolve_once(
    app_engine: AsyncEngine, system_engine: AsyncEngine
) -> None:
    shop = await Shop.create(app_engine)
    await shop.process(await shop.event("checkout"))
    await shop.process(await shop.event("checkout", status="resolved", received=T0))
    at = T0 + timedelta(minutes=10)
    await asyncio.gather(*(_sweep(shop, system_engine, at) for _ in range(3)))
    assert (
        len(await shop.rows("select 1 from incident_transitions where to_status = 'RESOLVED'")) == 1
    )
