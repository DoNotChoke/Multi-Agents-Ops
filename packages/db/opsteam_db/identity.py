"""Resolve an authenticated subject to a user and the tenants they belong to."""

from collections.abc import Mapping
from dataclasses import dataclass

from opsteam_core.ids import new_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class Identity:
    """A user and their roles per tenant; nothing here is scoped to a tenant yet."""

    user_id: str
    memberships: Mapping[str, tuple[str, ...]]


async def identify(engine: AsyncEngine, issuer: str, subject: str, email: str | None) -> Identity:
    """Return the user for (issuer, subject), creating it on first sight, with its memberships."""
    async with engine.begin() as conn:
        user_id = await conn.scalar(
            text("select app_user_for(:issuer, :subject, :email, :new_id)"),
            {"issuer": issuer, "subject": subject, "email": email, "new_id": new_id("usr")},
        )
        rows = await conn.execute(
            text("select tenant_id, roles from app_memberships_for(:user_id)"),
            {"user_id": user_id},
        )
        memberships = {tenant_id: tuple(roles) for tenant_id, roles in rows}
    return Identity(user_id=str(user_id), memberships=memberships)
