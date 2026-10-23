import pytest
from alembic import command
from alembic.config import Config
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import create_engine, identify, tenant_session
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


async def test_membership_lookup_returns_only_that_users_tenants(app_engine: AsyncEngine) -> None:
    a, b = new_id("tnt"), new_id("tnt")
    user, other = new_id("usr"), new_id("usr")
    async with app_engine.begin() as conn:
        for u in (user, other):
            await conn.execute(
                text("select app_user_for('i', :u, null, :u)"),
                {"u": u},
            )
    for tenant, members in ((a, (user, other)), (b, (user,))):
        async with tenant_session(app_engine, _ctx(tenant)) as session:
            await session.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
            )
            for u in members:
                await session.execute(
                    text("insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)"),
                    {"t": tenant, "u": u, "r": ["viewer"]},
                )
    async with app_engine.connect() as conn:
        lookup = text("select tenant_id, roles from app_memberships_for(:u)")
        assert sorted((await conn.execute(lookup, {"u": user})).all()) == sorted(
            [(a, ["viewer"]), (b, ["viewer"])]
        )
        assert (await conn.execute(lookup, {"u": other})).all() == [(a, ["viewer"])]
        assert await conn.scalar(text("select count(*) from memberships")) == 0


async def test_identify_creates_a_user_once_per_issuer_and_subject(app_engine: AsyncEngine) -> None:
    subject = new_id("usr")  # any unique string
    first = await identify(app_engine, "https://idp.test", subject, None)
    again = await identify(app_engine, "https://idp.test", subject, None)
    other_issuer = await identify(app_engine, "https://other.test", subject, None)
    assert first.user_id == again.user_id != other_issuer.user_id
    assert first.memberships == {}


async def test_app_cannot_write_users_directly(app_engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into users (user_id, oidc_issuer, oidc_subject) values (:u, 'i', 's')"
                ),
                {"u": new_id("usr")},
            )


async def test_tenant_sees_only_its_members_in_users(app_engine: AsyncEngine) -> None:
    a, b = new_id("tnt"), new_id("tnt")
    in_a = await identify(app_engine, "https://idp.test", new_id("usr"), None)
    in_b = await identify(app_engine, "https://idp.test", new_id("usr"), None)
    for tenant, user in ((a, in_a.user_id), (b, in_b.user_id)):
        async with tenant_session(app_engine, _ctx(tenant)) as session:
            await session.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
            )
            await session.execute(
                text("insert into memberships (tenant_id, user_id, roles) values (:t, :u, :r)"),
                {"t": tenant, "u": user, "r": ["viewer"]},
            )
    async with tenant_session(app_engine, _ctx(a)) as session:
        seen = (await session.scalars(text("select user_id from users"))).all()
    assert seen == [in_a.user_id]
    assert (await identify(app_engine, "https://idp.test", "x", None)).memberships == {}
