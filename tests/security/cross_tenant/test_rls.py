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


FACTORIES: dict[str, Factory] = {
    "tenants": _tenants,
    "memberships": _memberships,
    "integrations": _integrations,
    "services": _services,
    "service_dependencies": _service_dependencies,
    "runners": _runners,
    "audit_events": _audit_events,
    "secrets": _secrets,
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
