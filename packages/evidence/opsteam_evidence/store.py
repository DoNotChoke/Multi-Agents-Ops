"""Turn a tool's output into Evidence: redact, hash, compress, store, then record the row."""

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opsteam_contracts.common import TimeWindow
from opsteam_contracts.evidence import Evidence, RedactionStatus, SourceType
from opsteam_core.canonical import JsonValue, canonical_json
from opsteam_core.clock import Clock, SystemClock
from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError, RedactionFailed
from opsteam_core.ids import new_id
from opsteam_core.redact import redact_value
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsteam_evidence.objects import ObjectStore


class EvidenceNotFound(OpsteamError):
    code = "evidence.not_found"


class PayloadWithheld(OpsteamError):
    """Redaction failed when the evidence was stored, so there is no payload to read."""

    code = "evidence.withheld"


@dataclass(frozen=True)
class Provenance:
    tool_call_id: str
    tool: str
    source_type: SourceType
    integration_id: str | None
    incident_id: str | None
    source_query: dict[str, Any]
    time_window: TimeWindow
    summary: str
    truncated: bool
    observed_at: datetime | None = None


async def store(
    session: AsyncSession,
    objects: ObjectStore,
    ctx: RequestContext,
    payload: JsonValue,
    provenance: Provenance,
    clock: Clock = SystemClock(),  # noqa: B008
) -> Evidence:
    """Store `payload` for the session's tenant; on redaction failure only metadata is kept."""
    evidence_id = new_id("ev")
    try:
        clean, counts = redact_value(payload)
        body: bytes | None = canonical_json(clean)
        status = RedactionStatus.redacted if counts else RedactionStatus.none_needed
    except RedactionFailed:
        body, status = None, RedactionStatus.redaction_failed
    # A withheld payload hashes as empty: nothing was stored, and its secrets must not be hashed.
    content_hash = "sha256:" + hashlib.sha256(body or b"").hexdigest()
    key = None
    if body is not None:
        incident = provenance.incident_id or "adhoc"
        key = f"tenants/{ctx.tenant_id}/incidents/{incident}/{evidence_id}.json.gz"
        metadata = {"content_hash": content_hash, "redaction_status": status.value}
        # Object before row: a failed insert leaves an orphan object, never a dangling row.
        await objects.put(key, gzip.compress(body), metadata)
    evidence = Evidence(
        schema_version=1,
        evidence_id=evidence_id,
        tenant_id=ctx.tenant_id,
        incident_id=provenance.incident_id,
        integration_id=provenance.integration_id,
        source_type=provenance.source_type,
        tool=provenance.tool,
        tool_call_id=provenance.tool_call_id,
        source_query=provenance.source_query,
        time_window=provenance.time_window,
        observed_at=provenance.observed_at,
        collected_at=clock.now(),
        summary=provenance.summary,
        content_hash=content_hash,
        redaction_status=status,
        truncated=provenance.truncated,
        payload_reference=key,
    )
    await session.execute(
        text("""
            insert into evidence (evidence_id, tenant_id, incident_id, integration_id, source_type,
                                  tool, tool_call_id, source_query, time_window, observed_at,
                                  collected_at, summary, content_hash, redaction_status,
                                  truncated, payload_reference)
            values (:evidence_id, :tenant_id, :incident_id, :integration_id, :source_type, :tool,
                    :tool_call_id, cast(:source_query as jsonb), cast(:time_window as jsonb),
                    :observed_at, :collected_at, :summary, :content_hash, :redaction_status,
                    :truncated, :payload_reference)
        """),
        evidence.model_dump(exclude={"schema_version", "derived_from"})
        | {
            "source_query": json.dumps(evidence.source_query),
            "time_window": evidence.time_window.model_dump_json(),
        },
    )
    return evidence


async def load(session: AsyncSession, objects: ObjectStore, evidence_id: str) -> JsonValue:
    """Return the stored payload of one of the session tenant's evidence items."""
    row = (
        await session.execute(
            text("select payload_reference from evidence where evidence_id = :id"),
            {"id": evidence_id},
        )
    ).one_or_none()
    if row is None:
        raise EvidenceNotFound("evidence not found")
    if row.payload_reference is None:
        raise PayloadWithheld("payload was withheld because redaction failed")
    data, _ = await objects.get(row.payload_reference)
    loaded: JsonValue = json.loads(gzip.decompress(data))
    return loaded
