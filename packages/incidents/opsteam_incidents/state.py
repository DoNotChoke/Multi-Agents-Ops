"""The incident lifecycle: one table of legal moves and the only function allowed to make them."""

import json
from datetime import datetime

from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from opsteam_db import audit
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

OPEN = (
    "NEW", "TRIAGED", "INVESTIGATING", "DIAGNOSED", "INCONCLUSIVE", "REMEDIATION_PROPOSED",
    "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "MANUAL_ACTION_REQUIRED",
)  # fmt: skip
STATES = (*OPEN, "RESOLVED", "CLOSED")

_MOVES: dict[str, frozenset[str]] = {
    "NEW": frozenset({"TRIAGED"}),
    "TRIAGED": frozenset({"INVESTIGATING"}),
    "INVESTIGATING": frozenset({"DIAGNOSED", "INCONCLUSIVE"}),
    "DIAGNOSED": frozenset({"INVESTIGATING", "REMEDIATION_PROPOSED", "MANUAL_ACTION_REQUIRED"}),
    "INCONCLUSIVE": frozenset({"INVESTIGATING"}),
    "REMEDIATION_PROPOSED": frozenset({"AWAITING_APPROVAL"}),
    "AWAITING_APPROVAL": frozenset({"EXECUTING", "MANUAL_ACTION_REQUIRED"}),
    "EXECUTING": frozenset({"VERIFYING", "MANUAL_ACTION_REQUIRED"}),
    "VERIFYING": frozenset({"MANUAL_ACTION_REQUIRED"}),
    "MANUAL_ACTION_REQUIRED": frozenset(),
    # Reopening goes straight back to investigating.
    "RESOLVED": frozenset({"CLOSED", "INVESTIGATING"}),
    "CLOSED": frozenset(),
}
# Every open incident can resolve: auto-resolve when its alerts clear, or a verified fix.
MOVES = {s: targets | {"RESOLVED"} if s in OPEN else targets for s, targets in _MOVES.items()}


class InvalidTransition(OpsteamError):
    code = "incident.invalid_transition"


class IncidentNotFound(OpsteamError):
    code = "incident.not_found"


async def record_activity(
    session: AsyncSession,
    ctx: RequestContext,
    incident_id: str,
    kind: str,
    ref_id: str | None,
    at: datetime | None = None,
) -> None:
    """Append to the incident feed that live streams read after a `seq`.

    The tenant's audit lock is taken first, so each tenant's rows commit in `seq` order and a
    reader that has seen `seq` n can never later miss a smaller one.
    """
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:t, 0))"), {"t": ctx.tenant_id}
    )
    await session.execute(
        text("""
            insert into incident_activity (tenant_id, incident_id, type, ref_id, at)
            values (:t, :i, :kind, :ref, coalesce(:at, now()))
        """),
        {"t": ctx.tenant_id, "i": incident_id, "kind": kind, "ref": ref_id, "at": at},
    )


def check_transition(source: str, target: str) -> None:
    """Raise InvalidTransition unless the lifecycle table allows source -> target."""
    if target not in MOVES.get(source, frozenset()):
        raise InvalidTransition(f"{source} -> {target} is not allowed")


async def transition(
    session: AsyncSession,
    ctx: RequestContext,
    incident_id: str,
    target: str,
    reason: str,
    at: datetime,
) -> str:
    """Move an incident in the caller's transaction, record why, and return its previous state."""
    source = await session.scalar(
        text("select status from incidents where incident_id = :id for update"),
        {"id": incident_id},
    )
    if source is None:
        raise IncidentNotFound("incident not found")
    check_transition(source, target)
    await session.execute(
        text("""
            update incidents set status = :target,
              resolved_at = case :target when 'RESOLVED' then :at
                                         when 'INVESTIGATING' then null
                                         else resolved_at end
            where incident_id = :id
        """),
        {"id": incident_id, "target": target, "at": at},
    )
    actor = {"type": ctx.principal.type, "id": ctx.principal.id}
    await session.execute(
        text("""
            insert into incident_transitions
              (tenant_id, incident_id, from_status, to_status, reason, actor, at)
            values (:t, :id, :source, :target, :reason, cast(:actor as jsonb), :at)
        """),
        {
            "t": ctx.tenant_id,
            "id": incident_id,
            "source": source,
            "target": target,
            "reason": reason[:500],
            "actor": json.dumps(actor),
            "at": at,
        },
    )
    await record_activity(session, ctx, incident_id, "incident.transition", incident_id, at)
    resource = {"type": "incident", "incident_id": incident_id, "from": source, "to": target}
    await audit.record(session, ctx, "incident.transitioned", resource, "allowed")
    return str(source)
