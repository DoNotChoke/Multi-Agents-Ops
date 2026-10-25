"""Behaviour every ObjectStore must have: the fake here, RustFS when dev-up is running."""

import os
from collections.abc import Callable

import pytest
from opsteam_core.ids import new_id
from opsteam_evidence.objects import FakeObjectStore, ObjectNotFound, ObjectStore, S3ObjectStore
from pydantic import SecretStr

S3_ENDPOINT = os.environ.get("OPSTEAM_S3_ENDPOINT", "http://127.0.0.1:9000")
S3_ACCESS = os.environ.get("S3_ACCESS_KEY", "dev-only-s3-access")
S3_SECRET = os.environ.get("S3_SECRET_KEY", "dev-only-s3-secret")
BUCKET = "evidence"


def rustfs() -> S3ObjectStore:
    store = S3ObjectStore(S3_ENDPOINT, S3_ACCESS, SecretStr(S3_SECRET), BUCKET)
    # CI starts a bare RustFS; dev-up's init job has usually made the bucket already.
    try:
        store._s3.head_bucket(Bucket=BUCKET)
    except store._s3.exceptions.ClientError:
        store._s3.create_bucket(Bucket=BUCKET)
    return store


STORES: list[object] = [
    pytest.param(FakeObjectStore, id="fake"),
    pytest.param(rustfs, id="rustfs", marks=pytest.mark.integration),
]


@pytest.mark.parametrize("make", STORES)
async def test_round_trip_keeps_bytes_and_metadata(make: Callable[[], ObjectStore]) -> None:
    store, key = make(), f"tenants/{new_id('tnt')}/incidents/adhoc/{new_id('ev')}.json.gz"
    data = bytes(range(256)) * 4
    await store.put(
        key, data, {"content_hash": "sha256:" + "0" * 64, "redaction_status": "redacted"}
    )
    assert await store.get(key) == (
        data, {"content_hash": "sha256:" + "0" * 64, "redaction_status": "redacted"},
    )  # fmt: skip


@pytest.mark.parametrize("make", STORES)
async def test_missing_key_is_object_not_found(make: Callable[[], ObjectStore]) -> None:
    with pytest.raises(ObjectNotFound):
        await make().get(f"tenants/{new_id('tnt')}/missing.json.gz")
