import pytest
from alembic import command
from alembic.config import Config
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import create_engine, tenant_session
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )


def test_migrations_upgrade_downgrade_upgrade(alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")


async def test_app_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("select rolsuper, rolbypassrls from pg_roles where rolname = current_user")
            )
        ).one()
    assert tuple(row) == (False, False)


async def test_tenant_setting_is_local_to_the_transaction(app_engine: AsyncEngine) -> None:
    tenant = new_id("tnt")
    async with tenant_session(app_engine, _ctx(tenant)) as session:
        inside = await session.scalar(text("select current_setting('app.tenant_id', true)"))
    async with app_engine.connect() as conn:
        after = await conn.scalar(text("select current_setting('app.tenant_id', true)"))
    assert inside == tenant
    assert after in (None, "")


async def test_tenant_sees_only_its_own_row(app_engine: AsyncEngine) -> None:
    a, b = new_id("tnt"), new_id("tnt")
    for tenant in (a, b):
        async with tenant_session(app_engine, _ctx(tenant)) as session:
            await session.execute(
                text("insert into tenants (tenant_id, name) values (:t, :t)"), {"t": tenant}
            )
    async with tenant_session(app_engine, _ctx(a)) as session:
        seen = (await session.scalars(text("select tenant_id from tenants"))).all()
    assert seen == [a]


async def test_rls_blocks_writing_another_tenants_row(app_engine: AsyncEngine) -> None:
    a, b = new_id("tnt"), new_id("tnt")
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, _ctx(a)) as session:
            await session.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": b}
            )


def test_engine_url_password_is_hidden(migrated: URL) -> None:
    assert migrated.password is not None
    assert migrated.password not in repr(create_engine(migrated))
