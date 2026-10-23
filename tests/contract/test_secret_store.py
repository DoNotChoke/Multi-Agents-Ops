"""Behaviour every SecretStore must have; run here for the fake and in integration for the DB."""

from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db.secrets import FakeSecretStore, SecretNotFound, SecretStore
from pydantic import SecretStr

FAKE_SECRET = "dev-only-fake-bearer-123"


def ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
    )


async def check_contract(store: SecretStore, a: str, b: str) -> None:
    """`a` and `b` are two existing tenants."""
    ref = await store.put(ctx(a), SecretStr(FAKE_SECRET))
    assert ref.startswith("sec_")
    value = await store.get(ctx(a), ref)
    assert value.get_secret_value() == FAKE_SECRET
    assert FAKE_SECRET not in repr(value) and FAKE_SECRET not in str(value)

    for action in (store.get, store.delete):
        try:
            await action(ctx(b), ref)
        except SecretNotFound as exc:
            assert FAKE_SECRET not in str(exc)
        else:
            raise AssertionError(f"tenant b reached tenant a's secret via {action.__name__}")
    assert (await store.get(ctx(a), ref)).get_secret_value() == FAKE_SECRET

    await store.delete(ctx(a), ref)
    try:
        await store.get(ctx(a), ref)
    except SecretNotFound:
        pass
    else:
        raise AssertionError("deleted secret is still readable")


async def test_fake_store_contract() -> None:
    await check_contract(FakeSecretStore(), new_id("tnt"), new_id("tnt"))
