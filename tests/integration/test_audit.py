import asyncio
from collections.abc import AsyncIterator

import pytest
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import audit, create_engine, tenant_session
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal=Principal(type="user", id=new_id("usr")),
        roles=(),
        scopes=frozenset(),
    )


@pytest.fixture
async def migrator_engine(migrated: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated)
    yield engine
    await engine.dispose()


async def _tenant_with_events(engine: AsyncEngine, count: int) -> tuple[str, list[str]]:
    tenant = new_id("tnt")
    async with tenant_session(engine, _ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
    ids = []
    for i in range(count):
        async with tenant_session(engine, _ctx(tenant)) as s:
            ids.append(
                await audit.record(
                    s, _ctx(tenant), "test.event", {"type": "n", "id": str(i)}, "allowed"
                )
            )
    return tenant, ids


async def _verify(engine: AsyncEngine, tenant: str) -> str | None:
    async with tenant_session(engine, _ctx(tenant)) as s:
        return await audit.verify_chain(s)


async def _tamper(engine: AsyncEngine, tenant: str, sql: str, audit_id: str) -> None:
    """As the table owner, bypass the append-only trigger the way an attacker with DDL could."""
    async with engine.begin() as conn:
        await conn.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": tenant})
        await conn.execute(text("alter table audit_events disable trigger audit_append_only"))
        await conn.execute(text(sql), {"id": audit_id})
        await conn.execute(text("alter table audit_events enable trigger audit_append_only"))


async def test_intact_chain_verifies(app_engine: AsyncEngine) -> None:
    tenant, _ = await _tenant_with_events(app_engine, 3)
    assert await _verify(app_engine, tenant) is None


async def test_modified_row_is_detected(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    tenant, ids = await _tenant_with_events(app_engine, 3)
    await _tamper(
        migrator_engine,
        tenant,
        "update audit_events set outcome = 'denied' where audit_id = :id",
        ids[1],
    )
    assert await _verify(app_engine, tenant) == ids[1]


async def test_deleted_row_is_detected(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    tenant, ids = await _tenant_with_events(app_engine, 3)
    await _tamper(migrator_engine, tenant, "delete from audit_events where audit_id = :id", ids[1])
    assert await _verify(app_engine, tenant) == ids[2]


@pytest.mark.parametrize(
    "sql", ["update audit_events set outcome = 'x'", "delete from audit_events"]
)
async def test_app_cannot_change_audit(app_engine: AsyncEngine, sql: str) -> None:
    tenant, _ = await _tenant_with_events(app_engine, 1)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(app_engine, _ctx(tenant)) as s:
            await s.execute(text(sql))


@pytest.mark.parametrize(
    "sql",
    ["update audit_events set outcome = 'x'", "delete from audit_events", "truncate audit_events"],
)
async def test_trigger_blocks_even_the_owner(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, sql: str
) -> None:
    tenant, _ = await _tenant_with_events(app_engine, 1)
    with pytest.raises(DBAPIError, match="append-only"):
        async with migrator_engine.begin() as conn:
            await conn.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": tenant})
            await conn.execute(text(sql))


async def test_concurrent_records_keep_one_chain(app_engine: AsyncEngine) -> None:
    tenant, _ = await _tenant_with_events(app_engine, 0)

    async def write(i: int) -> None:
        async with tenant_session(app_engine, _ctx(tenant)) as s:
            await audit.record(
                s, _ctx(tenant), "test.event", {"type": "n", "id": str(i)}, "allowed"
            )

    await asyncio.gather(*(write(i) for i in range(20)))
    async with tenant_session(app_engine, _ctx(tenant)) as s:
        count = await s.scalar(text("select count(*) from audit_events"))
    assert count == 20
    assert await _verify(app_engine, tenant) is None
