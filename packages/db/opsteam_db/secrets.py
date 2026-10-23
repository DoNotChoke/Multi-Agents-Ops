"""Credential storage: callers keep only a `credentials_ref`, never the value."""

import base64
import binascii
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_core.ids import new_id
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opsteam_db.session import tenant_session

_NONCE_BYTES = 12


class SecretNotFound(OpsteamError):
    """No secret with this reference exists for the caller's tenant."""

    code = "secret.not_found"


class SecretUnreadable(OpsteamError):
    """The stored ciphertext does not decrypt for this tenant and reference."""

    code = "secret.unreadable"


class SecretStore(Protocol):
    """Where credential values live; production backs this with Vault or a cloud KMS."""

    async def put(self, ctx: RequestContext, value: SecretStr) -> str: ...
    async def get(self, ctx: RequestContext, ref: str) -> SecretStr: ...
    async def delete(self, ctx: RequestContext, ref: str) -> None: ...


class FakeSecretStore:
    """In-memory store for unit tests."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], SecretStr] = {}

    async def put(self, ctx: RequestContext, value: SecretStr) -> str:
        ref = new_id("sec")
        self._values[(ctx.tenant_id, ref)] = value
        return ref

    async def get(self, ctx: RequestContext, ref: str) -> SecretStr:
        try:
            return self._values[(ctx.tenant_id, ref)]
        except KeyError:
            raise SecretNotFound("secret not found") from None

    async def delete(self, ctx: RequestContext, ref: str) -> None:
        if self._values.pop((ctx.tenant_id, ref), None) is None:
            raise SecretNotFound("secret not found")


class DbSecretStore:
    """AES-256-GCM ciphertext in the `secrets` table, bound to its tenant and reference."""

    def __init__(self, engine: AsyncEngine, key: SecretStr) -> None:
        try:
            raw = base64.b64decode(key.get_secret_value(), validate=True)
        except binascii.Error:
            raw = b""
        if len(raw) != 32:
            raise ValueError("secrets key must be 32 bytes, base64-encoded")
        self._engine = engine
        self._aead = AESGCM(raw)

    @staticmethod
    def _aad(ctx: RequestContext, ref: str) -> bytes:
        # Ciphertext copied to another tenant or reference fails authentication.
        return f"{ctx.tenant_id}|{ref}".encode()

    async def put(self, ctx: RequestContext, value: SecretStr) -> str:
        ref = new_id("sec")
        nonce = os.urandom(_NONCE_BYTES)
        blob = nonce + self._aead.encrypt(
            nonce, value.get_secret_value().encode(), self._aad(ctx, ref)
        )
        async with tenant_session(self._engine, ctx) as session:
            await session.execute(
                text("""
                    insert into secrets (credentials_ref, tenant_id, ciphertext)
                    values (:ref, :tenant_id, :blob)
                """),
                {"ref": ref, "tenant_id": ctx.tenant_id, "blob": blob},
            )
        return ref

    async def get(self, ctx: RequestContext, ref: str) -> SecretStr:
        async with tenant_session(self._engine, ctx) as session:
            blob = await session.scalar(
                text("select ciphertext from secrets where credentials_ref = :ref"), {"ref": ref}
            )
        if blob is None:
            raise SecretNotFound("secret not found")
        try:
            plain = self._aead.decrypt(
                blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], self._aad(ctx, ref)
            )
        except InvalidTag:
            raise SecretUnreadable("secret cannot be decrypted") from None
        return SecretStr(plain.decode())

    async def delete(self, ctx: RequestContext, ref: str) -> None:
        async with tenant_session(self._engine, ctx) as session:
            deleted = await session.scalar(
                text("delete from secrets where credentials_ref = :ref returning credentials_ref"),
                {"ref": ref},
            )
        if deleted is None:
            raise SecretNotFound("secret not found")
