"""Object storage for evidence payloads: a Protocol, an in-memory fake and an S3 client."""

import asyncio
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from opsteam_core.errors import OpsteamError
from pydantic import SecretStr

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client


class ObjectNotFound(OpsteamError):
    code = "object.not_found"


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None: ...

    async def get(self, key: str) -> tuple[bytes, dict[str, str]]:
        """Return the object's bytes and metadata, or raise ObjectNotFound."""
        ...


class FakeObjectStore:
    """Keeps objects in a dict; for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None:
        self.objects[key] = (data, dict(metadata))

    async def get(self, key: str) -> tuple[bytes, dict[str, str]]:
        if key not in self.objects:
            raise ObjectNotFound(f"no object {key!r}")
        data, metadata = self.objects[key]
        return data, dict(metadata)


class S3ObjectStore:
    """S3-compatible store (RustFS in dev); boto3 is blocking, so calls run in a worker thread."""

    def __init__(
        self, endpoint_url: str, access_key: str, secret_key: SecretStr, bucket: str
    ) -> None:
        self.bucket = bucket
        self._s3: S3Client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key.get_secret_value(),
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, connect_timeout=2, read_timeout=10),
        )

    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None:
        await asyncio.to_thread(
            self._s3.put_object, Bucket=self.bucket, Key=key, Body=data, Metadata=metadata
        )

    async def get(self, key: str) -> tuple[bytes, dict[str, str]]:
        return await asyncio.to_thread(self._get, key)

    def _get(self, key: str) -> tuple[bytes, dict[str, str]]:
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFound(f"no object {key!r}") from exc
            raise
        return obj["Body"].read(), obj["Metadata"]
