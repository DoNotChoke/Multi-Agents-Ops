"""Append-only, hash-chained audit events; one chain per tenant."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from opsteam_core.canonical import canonical_json
from opsteam_core.context import RequestContext
from opsteam_core.ids import new_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = "audit_id, tenant_id, actor, action, resource, outcome, request_id, at, prev_hash"


def _row_hash(row: dict[str, Any]) -> str:
    content = {k: row[k] for k in _COLUMNS.split(", ")}
    content["at"] = row["at"].astimezone(UTC).isoformat()
    return hashlib.sha256((row["prev_hash"] or "").encode() + canonical_json(content)).hexdigest()


async def record(
    session: AsyncSession,
    ctx: RequestContext,
    action: str,
    resource: dict[str, Any],
    outcome: str,
) -> str:
    """Append one event for ctx's tenant in the caller's transaction and return its id."""
    # Serialise writers of one tenant so two events never chain onto the same predecessor.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:tenant_id, 0))"),
        {"tenant_id": ctx.tenant_id},
    )
    prev_hash = await session.scalar(
        text("select row_hash from audit_events order by seq desc limit 1")
    )
    row: dict[str, Any] = {
        "audit_id": new_id("aud"),
        "tenant_id": ctx.tenant_id,
        "actor": {"type": ctx.principal.type, "id": ctx.principal.id},
        "action": action,
        "resource": resource,
        "outcome": outcome,
        "request_id": ctx.request_id,
        "at": datetime.now(UTC),
        "prev_hash": prev_hash,
    }
    await session.execute(
        text(
            f"insert into audit_events ({_COLUMNS}, row_hash) values (:audit_id, :tenant_id,"  # noqa: S608
            " cast(:actor as jsonb), :action, cast(:resource as jsonb), :outcome, :request_id,"
            " :at, :prev_hash, :row_hash)"
        ),
        row
        | {
            "actor": canonical_json(row["actor"]).decode(),
            "resource": canonical_json(resource).decode(),
            "row_hash": _row_hash(row),
        },
    )
    return str(row["audit_id"])


async def verify_chain(session: AsyncSession) -> str | None:
    """Return the id of the first event of the tenant's chain that fails verification, else None."""
    rows = await session.execute(
        text(f"select {_COLUMNS}, row_hash from audit_events order by seq")  # noqa: S608
    )
    previous = None
    for row in rows.mappings():
        if row["prev_hash"] != previous or row["row_hash"] != _row_hash(dict(row)):
            return str(row["audit_id"])
        previous = row["row_hash"]
    return None
