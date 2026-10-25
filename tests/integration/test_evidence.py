import gzip
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_contracts.common import TimeWindow
from opsteam_contracts.evidence import RedactionStatus, SourceType
from opsteam_core.canonical import JsonValue
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_evidence.objects import FakeObjectStore
from opsteam_evidence.store import EvidenceNotFound, PayloadWithheld, Provenance, load, store
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

FAKE_TOKEN = "dev-only-evidence-token-9f2"
END = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


async def _tenant_with_call(engine: AsyncEngine) -> tuple[str, str]:
    tenant, call = new_id("tnt"), new_id("tc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        await s.execute(
            text("""
                insert into tool_calls (tool_call_id, tenant_id, tool, request, status)
                values (:c, :t, 'metrics.query_range', '{}', 'ok')
            """),
            {"c": call, "t": tenant},
        )
    return tenant, call


def _provenance(call: str) -> Provenance:
    return Provenance(
        tool_call_id=call,
        tool="metrics.query_range",
        source_type=SourceType.metrics,
        integration_id=None,
        incident_id=None,
        source_query={"query_template": "http_latency_p95", "params": {"service": "checkout"}},
        time_window=TimeWindow(start_at=END - timedelta(hours=1), end_at=END),
        summary="1 series, 1 with anomalies",
        truncated=False,
    )


async def _store(engine: AsyncEngine, tenant: str, call: str, payload: JsonValue) -> Any:
    objects = FakeObjectStore()
    async with tenant_session(engine, system_ctx(tenant)) as s:
        ev = await store(s, objects, system_ctx(tenant), payload, _provenance(call))
    return ev, objects


async def test_secret_never_reaches_the_object(app_engine: AsyncEngine) -> None:
    tenant, call = await _tenant_with_call(app_engine)
    payload = {"series": [{"labels": {"note": f"Authorization: Bearer {FAKE_TOKEN}"}}]}
    ev, objects = await _store(app_engine, tenant, call, payload)
    assert ev.redaction_status is RedactionStatus.redacted
    assert ev.payload_reference == f"tenants/{tenant}/incidents/adhoc/{ev.evidence_id}.json.gz"
    data, metadata = objects.objects[ev.payload_reference]
    assert FAKE_TOKEN.encode() not in gzip.decompress(data)
    assert metadata == {"content_hash": ev.content_hash, "redaction_status": "redacted"}
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        loaded = await load(s, objects, ev.evidence_id)
    assert "[REDACTED:" in json.dumps(loaded) and FAKE_TOKEN not in json.dumps(loaded)


async def test_clean_payload_round_trips_with_stable_hash(app_engine: AsyncEngine) -> None:
    tenant, call = await _tenant_with_call(app_engine)
    payload: JsonValue = {"series": [{"stats": {"max": 2.4, "points": 29}}], "truncated": False}
    ev, objects = await _store(app_engine, tenant, call, payload)
    assert ev.redaction_status is RedactionStatus.none_needed
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        assert await load(s, objects, ev.evidence_id) == payload
        row = (await s.execute(text("select * from evidence"))).mappings().one()
    assert (row["content_hash"], row["tool_call_id"]) == (ev.content_hash, call)
    other_tenant, other_call = await _tenant_with_call(app_engine)
    again, _ = await _store(app_engine, other_tenant, other_call, payload)
    assert again.content_hash == ev.content_hash


async def test_redaction_failure_stores_no_object(app_engine: AsyncEngine) -> None:
    tenant, call = await _tenant_with_call(app_engine)
    nested: JsonValue = "leaf"
    for _ in range(5000):  # deeper than the redactor can walk
        nested = [nested]
    ev, objects = await _store(app_engine, tenant, call, {"payload": nested})
    assert ev.redaction_status is RedactionStatus.redaction_failed
    assert ev.payload_reference is None and objects.objects == {}
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        with pytest.raises(PayloadWithheld):
            await load(s, objects, ev.evidence_id)


async def test_other_tenants_evidence_is_not_found(app_engine: AsyncEngine) -> None:
    tenant, call = await _tenant_with_call(app_engine)
    ev, objects = await _store(app_engine, tenant, call, {"series": []})
    stranger, _ = await _tenant_with_call(app_engine)
    async with tenant_session(app_engine, system_ctx(stranger)) as s:
        with pytest.raises(EvidenceNotFound):
            await load(s, objects, ev.evidence_id)


async def test_evidence_is_append_only_for_the_app(app_engine: AsyncEngine) -> None:
    tenant, call = await _tenant_with_call(app_engine)
    await _store(app_engine, tenant, call, {"series": []})
    for statement in ("update evidence set summary = 'x'", "delete from evidence"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session(app_engine, system_ctx(tenant)) as s:
                await s.execute(text(statement))


async def test_deleting_an_integration_keeps_its_history(app_engine: AsyncEngine) -> None:
    tenant, _ = await _tenant_with_call(app_engine)
    integration, call = new_id("int"), new_id("tc")
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        await s.execute(
            text("""
                insert into integrations (integration_id, tenant_id, type, name, mode)
                values (:i, :t, 'prometheus', 'p', 'direct')
            """),
            {"i": integration, "t": tenant},
        )
        await s.execute(
            text("""
                insert into tool_calls (tool_call_id, tenant_id, tool, integration_id, request,
                                        status)
                values (:c, :t, 'metrics.query_range', :i, '{}', 'ok')
            """),
            {"c": call, "t": tenant, "i": integration},
        )
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        await s.execute(
            text("delete from integrations where integration_id = :i"), {"i": integration}
        )
        kept = await s.execute(
            text("select integration_id from tool_calls where tool_call_id = :c"), {"c": call}
        )
        assert kept.scalar_one() is None
