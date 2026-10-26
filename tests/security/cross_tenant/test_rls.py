"""Tenant A must not read, change or create rows of another tenant in any table with tenant_id."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration

type Row = tuple[str, dict[str, Any]]
# Inserts the row's prerequisites for `tenant`, then returns the insert for one row of the table.
type Factory = Callable[[AsyncSession, str], Awaitable[Row]]

BLOCKED = "row-level security|permission denied"


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )


async def _tenants(s: AsyncSession, t: str) -> Row:
    return "insert into tenants (tenant_id, name) values (:t, 'x')", {"t": t}


async def _memberships(s: AsyncSession, t: str) -> Row:
    user = new_id("usr")
    await s.execute(
        text("select app_user_for('test', :u, null, :u)"),
        {"u": user},
    )
    return (
        "insert into memberships (tenant_id, user_id, roles) values (:t, :u, '{viewer}')",
        {"t": t, "u": user},
    )


async def _integrations(s: AsyncSession, t: str) -> Row:
    return (
        "insert into integrations (integration_id, tenant_id, type, name, mode)"
        " values (:id, :t, 'prometheus', :id, 'direct')",
        {"id": new_id("int"), "t": t},
    )


async def _services(s: AsyncSession, t: str) -> Row:
    return (
        "insert into services (service_id, tenant_id, name, environment)"
        " values (:id, :t, :id, 'prod')",
        {"id": new_id("svc"), "t": t},
    )


async def _service_dependencies(s: AsyncSession, t: str) -> Row:
    ids = []
    for _ in range(2):
        sql, params = await _services(s, t)
        await s.execute(text(sql), params)
        ids.append(params["id"])
    return (
        "insert into service_dependencies (tenant_id, service_id, depends_on_service_id)"
        " values (:t, :a, :b)",
        {"t": t, "a": ids[0], "b": ids[1]},
    )


async def _runners(s: AsyncSession, t: str) -> Row:
    return (
        "insert into runners (runner_id, tenant_id, name, status) values (:id, :t, :id, 'pending')",
        {"id": new_id("rnr"), "t": t},
    )


async def _audit_events(s: AsyncSession, t: str) -> Row:
    return (
        "insert into audit_events (audit_id, tenant_id, actor, action, resource, outcome, row_hash)"
        " values (:id, :t, '{}', 'test', '{}', 'allowed', 'x')",
        {"id": new_id("aud"), "t": t},
    )


async def _secrets(s: AsyncSession, t: str) -> Row:
    return (
        "insert into secrets (credentials_ref, tenant_id, ciphertext)"
        " values (:id, :t, decode('00', 'hex'))",
        {"id": new_id("sec"), "t": t},
    )


async def _tool_calls(s: AsyncSession, t: str) -> Row:
    return (
        "insert into tool_calls (tool_call_id, tenant_id, tool, request, status)"
        " values (:id, :t, 'metrics.query_range', '{}', 'ok')",
        {"id": new_id("tc"), "t": t},
    )


async def _evidence(s: AsyncSession, t: str) -> Row:
    sql, params = await _tool_calls(s, t)
    await s.execute(text(sql), params)
    return (
        "insert into evidence (evidence_id, tenant_id, source_type, tool, tool_call_id,"
        " source_query, time_window, collected_at, summary, content_hash, redaction_status,"
        " truncated, payload_reference)"
        " values (:id, :t, 'metrics', 'metrics.query_range', :tc, '{}', '{}', now(), '',"
        " :hash, 'none_needed', false, 'k')",
        {"id": new_id("ev"), "t": t, "tc": params["id"], "hash": "sha256:" + "0" * 64},
    )


async def _events(s: AsyncSession, t: str) -> Row:
    return (
        "insert into events (event_id, tenant_id, source_type, fingerprint, status, title,"
        " labels, annotations, starts_at, received_at)"
        " values (:id, :t, 'alertmanager', :fp, 'firing', 'x', '{}', '{}', now(), now())",
        {"id": new_id("evt"), "t": t, "fp": "0" * 64},
    )


async def _outbox(s: AsyncSession, t: str) -> Row:
    return (
        "insert into outbox (tenant_id, topic, payload) values (:t, 'event.received', '{}')",
        {"t": t},
    )


async def _incidents(s: AsyncSession, t: str) -> Row:
    return (
        "insert into incidents (incident_id, tenant_id, source, title, status, severity,"
        " opened_at, detected_at, created_by)"
        " values (:id, :t, 'alert', 'x', 'NEW', 'medium', now(), now(), 'system:test')",
        {"id": new_id("inc"), "t": t},
    )


async def _own_incident(s: AsyncSession, t: str) -> str:
    sql, params = await _incidents(s, t)
    await s.execute(text(sql), params)
    return str(params["id"])


async def _incident_events(s: AsyncSession, t: str) -> Row:
    incident = await _own_incident(s, t)
    sql, params = await _events(s, t)
    await s.execute(text(sql), params)
    return (
        "insert into incident_events (incident_id, event_id, tenant_id) values (:i, :e, :t)",
        {"i": incident, "e": params["id"], "t": t},
    )


async def _incident_transitions(s: AsyncSession, t: str) -> Row:
    return (
        "insert into incident_transitions (tenant_id, incident_id, to_status, reason, actor, at)"
        " values (:t, :i, 'NEW', 'x', '{}', now())",
        {"t": t, "i": await _own_incident(s, t)},
    )


async def _incident_activity(s: AsyncSession, t: str) -> Row:
    return (
        "insert into incident_activity (tenant_id, incident_id, type) values (:t, :i, 'x')",
        {"t": t, "i": await _own_incident(s, t)},
    )


FACTORIES: dict[str, Factory] = {
    "tenants": _tenants,
    "memberships": _memberships,
    "integrations": _integrations,
    "services": _services,
    "service_dependencies": _service_dependencies,
    "runners": _runners,
    "audit_events": _audit_events,
    "secrets": _secrets,
    "tool_calls": _tool_calls,
    "evidence": _evidence,
    "events": _events,
    "outbox": _outbox,
    "incidents": _incidents,
    "incident_events": _incident_events,
    "incident_transitions": _incident_transitions,
    "incident_activity": _incident_activity,
}


async def _new_tenant(engine: AsyncEngine) -> str:
    tenant = new_id("tnt")
    async with tenant_session(engine, _ctx(tenant)) as s:
        sql, params = await _tenants(s, tenant)
        await s.execute(text(sql), params)
    return tenant


async def _seed(engine: AsyncEngine, table: str) -> str:
    """Create a tenant owning one row of `table` and return its id."""
    tenant = await _new_tenant(engine)
    if table != "tenants":
        async with tenant_session(engine, _ctx(tenant)) as s:
            sql, params = await FACTORIES[table](s, tenant)
            await s.execute(text(sql), params)
    return tenant


async def _tenant_tables(engine: AsyncEngine) -> dict[str, tuple[bool, bool, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("""
                select c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (select count(*) from pg_policies p where p.tablename = c.relname)
                from pg_class c
                join information_schema.columns col
                  on col.table_name = c.relname and col.table_schema = 'public'
                where col.column_name = 'tenant_id' and c.relkind = 'r'
            """)
        )
        return {name: (rls, force, policies) for name, rls, force, policies in rows}


async def test_every_tenant_table_has_forced_rls_and_a_factory(app_engine: AsyncEngine) -> None:
    tables = await _tenant_tables(app_engine)
    assert set(tables) == set(FACTORIES), "add a factory for each new table with tenant_id"
    for name, (rls, force, policies) in tables.items():
        assert rls and force and policies >= 1, f"{name} is not isolated by RLS"


@pytest.mark.parametrize("table", sorted(FACTORIES))
async def test_cannot_read_other_tenant(app_engine: AsyncEngine, table: str) -> None:
    a, b = await _new_tenant(app_engine), await _seed(app_engine, table)
    query = text(f"select count(*) from {table} where tenant_id = :b")  # noqa: S608
    async with tenant_session(app_engine, _ctx(b)) as s:
        assert await s.scalar(query, {"b": b}) == 1
    async with tenant_session(app_engine, _ctx(a)) as s:
        assert await s.scalar(query, {"b": b}) == 0


@pytest.mark.parametrize("table", sorted(FACTORIES))
@pytest.mark.parametrize("verb", ["update {t} set tenant_id = tenant_id", "delete from {t}"])
async def test_cannot_change_other_tenant(app_engine: AsyncEngine, table: str, verb: str) -> None:
    a, b = await _new_tenant(app_engine), await _seed(app_engine, table)
    statement = text(verb.format(t=table) + " where tenant_id = :b")
    try:
        async with tenant_session(app_engine, _ctx(a)) as s:
            result = await s.execute(statement, {"b": b})
        assert result.rowcount == 0  # type: ignore[attr-defined]
    except DBAPIError as exc:
        assert "permission denied" in str(exc)


@pytest.mark.parametrize("table", sorted(FACTORIES))
async def test_cannot_insert_for_other_tenant(app_engine: AsyncEngine, table: str) -> None:
    a = await _new_tenant(app_engine)
    other = new_id("tnt") if table == "tenants" else await _new_tenant(app_engine)
    async with tenant_session(app_engine, _ctx(other)) as s:
        sql, params = await FACTORIES[table](s, other)
    with pytest.raises(DBAPIError, match=BLOCKED):
        async with tenant_session(app_engine, _ctx(a)) as s:
            await s.execute(text(sql), params)


@pytest.mark.parametrize("table", sorted(FACTORIES))
async def test_cannot_move_own_row_to_other_tenant(app_engine: AsyncEngine, table: str) -> None:
    a, b = await _seed(app_engine, table), await _new_tenant(app_engine)
    with pytest.raises(DBAPIError, match=BLOCKED):
        async with tenant_session(app_engine, _ctx(a)) as s:
            await s.execute(
                text(f"update {table} set tenant_id = :b where tenant_id = :a"),  # noqa: S608
                {"a": a, "b": b},
            )
