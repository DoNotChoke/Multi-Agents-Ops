import asyncio
from datetime import UTC, datetime

import pytest
from opsteam_core.ids import new_id
from opsteam_db import audit, tenant_session
from opsteam_incidents.state import IncidentNotFound, InvalidTransition, transition
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


async def _incident(engine: AsyncEngine, status: str = "NEW") -> tuple[str, str]:
    tenant, incident = new_id("tnt"), new_id("inc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        await s.execute(
            text("""
                insert into incidents (incident_id, tenant_id, source, title, status, severity,
                                       opened_at, detected_at, created_by)
                values (:i, :t, 'alert', 'checkout slow', :s, 'medium', :at, :at, 'system:test')
            """),
            {"i": incident, "t": tenant, "s": status, "at": AT},
        )
    return tenant, incident


async def test_a_transition_records_history_activity_and_audit(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine)
    ctx = system_ctx(tenant)
    async with tenant_session(app_engine, ctx) as s:
        previous = await transition(s, ctx, incident, "TRIAGED", "first notification sent", AT)
    assert previous == "NEW"
    async with tenant_session(app_engine, ctx) as s:
        assert await s.scalar(text("select status from incidents")) == "TRIAGED"
        history = (await s.execute(text("select * from incident_transitions"))).one()
        activity = (await s.execute(text("select type, ref_id from incident_activity"))).one()
        action = await s.scalar(text("select action from audit_events"))
        assert await audit.verify_chain(s) is None
    assert (history.from_status, history.to_status, history.at) == ("NEW", "TRIAGED", AT)
    assert history.reason == "first notification sent"
    assert history.actor == {"type": "system", "id": "test"}
    assert tuple(activity) == ("incident.transition", incident)
    assert action == "incident.transitioned"


async def test_resolving_stamps_and_reopening_clears_resolved_at(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine, "TRIAGED")
    ctx = system_ctx(tenant)
    async with tenant_session(app_engine, ctx) as s:
        await transition(s, ctx, incident, "RESOLVED", "auto_resolved", AT)
        assert await s.scalar(text("select resolved_at from incidents")) == AT
        await transition(s, ctx, incident, "INVESTIGATING", "reopened", AT)
        assert await s.scalar(text("select resolved_at from incidents")) is None


async def test_invalid_transitions_change_nothing(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine)
    ctx = system_ctx(tenant)
    with pytest.raises(InvalidTransition):
        async with tenant_session(app_engine, ctx) as s:
            await transition(s, ctx, incident, "CLOSED", "skip ahead", AT)
    async with tenant_session(app_engine, ctx) as s:
        assert await s.scalar(text("select status from incidents")) == "NEW"
        assert await s.scalar(text("select count(*) from incident_transitions")) == 0


async def test_other_tenants_incidents_are_not_found(app_engine: AsyncEngine) -> None:
    _, incident = await _incident(app_engine)
    other = system_ctx(new_id("tnt"))
    with pytest.raises(IncidentNotFound):
        async with tenant_session(app_engine, other) as s:
            await transition(s, other, incident, "TRIAGED", "x", AT)


async def test_two_racing_transitions_have_one_winner(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine)
    ctx = system_ctx(tenant)

    async def triage() -> str:
        async with tenant_session(app_engine, ctx) as s:
            previous = await transition(s, ctx, incident, "TRIAGED", "race", AT)
            await asyncio.sleep(0.2)  # hold the row lock so the other one must wait
            return previous

    results = await asyncio.gather(triage(), triage(), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["InvalidTransition", "str"]
    async with tenant_session(app_engine, ctx) as s:
        assert await s.scalar(text("select count(*) from incident_transitions")) == 1
