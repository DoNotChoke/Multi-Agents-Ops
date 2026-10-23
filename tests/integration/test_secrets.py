import base64
import os

import pytest
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_db.secrets import DbSecretStore, SecretUnreadable
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.contract.test_secret_store import FAKE_SECRET, check_contract, ctx

pytestmark = pytest.mark.integration

KEY = base64.b64encode(os.urandom(32)).decode()


async def _tenants(engine: AsyncEngine, n: int = 2) -> list[str]:
    ids = [new_id("tnt") for _ in range(n)]
    for t in ids:
        async with tenant_session(engine, ctx(t)) as s:
            await s.execute(
                text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": t}
            )
    return ids


async def test_db_store_contract(app_engine: AsyncEngine) -> None:
    a, b = await _tenants(app_engine)
    await check_contract(DbSecretStore(app_engine, SecretStr(KEY)), a, b)


async def test_ciphertext_holds_no_plaintext(app_engine: AsyncEngine) -> None:
    (a,) = await _tenants(app_engine, 1)
    ref = await DbSecretStore(app_engine, SecretStr(KEY)).put(ctx(a), SecretStr(FAKE_SECRET))
    async with tenant_session(app_engine, ctx(a)) as s:
        blob = await s.scalar(
            text("select ciphertext from secrets where credentials_ref = :r"), {"r": ref}
        )
    assert FAKE_SECRET.encode() not in blob


@pytest.mark.parametrize("move_to", ["other_tenant", "other_ref"])
async def test_copied_ciphertext_does_not_decrypt(app_engine: AsyncEngine, move_to: str) -> None:
    a, b = await _tenants(app_engine)
    store = DbSecretStore(app_engine, SecretStr(KEY))
    ref = await store.put(ctx(a), SecretStr(FAKE_SECRET))
    async with tenant_session(app_engine, ctx(a)) as s:
        blob = await s.scalar(
            text("select ciphertext from secrets where credentials_ref = :r"), {"r": ref}
        )
    target = b if move_to == "other_tenant" else a
    stolen = new_id("sec")
    async with tenant_session(app_engine, ctx(target)) as s:
        await s.execute(
            text(
                "insert into secrets (credentials_ref, tenant_id, ciphertext) values (:r, :t, :c)"
            ),
            {"r": stolen, "t": target, "c": blob},
        )
    with pytest.raises(SecretUnreadable) as exc:
        await store.get(ctx(target), stolen)
    assert FAKE_SECRET not in str(exc.value)


async def test_wrong_key_is_unreadable(app_engine: AsyncEngine) -> None:
    (a,) = await _tenants(app_engine, 1)
    ref = await DbSecretStore(app_engine, SecretStr(KEY)).put(ctx(a), SecretStr(FAKE_SECRET))
    other_key = SecretStr(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(SecretUnreadable):
        await DbSecretStore(app_engine, other_key).get(ctx(a), ref)


@pytest.mark.parametrize("bad", ["", "not-base64!", base64.b64encode(b"short").decode()])
def test_key_must_be_32_bytes(app_engine: AsyncEngine, bad: str) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        DbSecretStore(app_engine, SecretStr(bad))
