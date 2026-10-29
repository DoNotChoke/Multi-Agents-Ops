import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_db import create_engine, tenant_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from tests.api_helpers import SIGNER, api_client, system_ctx
from tests.integration.test_incidents_api import T0, _alert, _tenant

pytestmark = pytest.mark.integration


def _headers(subject: str, tenant: str, ttl_s: float = 2.0, **extra: str) -> dict[str, str]:
    """A token that expires soon, so the stream closes by itself and the test can finish."""
    token = SIGNER.token(sub=subject, exp=int(time.time() + ttl_s))
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant} | extra


def _read(client: Any, path: str, headers: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    """Every SSE message until the server closes the stream, ignoring heartbeats."""
    events: list[dict[str, Any]] = []
    with client.stream("GET", f"/api/v1{path}", headers=headers) as resp:
        if resp.status_code != 200:
            resp.read()
            return resp.status_code, [resp.json()]
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-store"
        message: dict[str, Any] = {}
        for line in resp.iter_lines():
            if line == "":
                if message.get("event") not in (None, "heartbeat"):
                    events.append(message)
                message = {}
            elif ":" in line and not line.startswith(":"):
                key, _, value = line.partition(": ")
                message[key] = json.loads(value) if key == "data" else value
    return 200, events


def _fast(client: Any) -> Any:
    client.app.state.stream_poll_s = 0.05
    client.app.state.stream_heartbeat_s = 0.2
    return client


async def _incident(engine: AsyncEngine, tenant: str) -> str:
    await _alert(engine, tenant, "checkout", "HighLatencyP95", T0)
    async with tenant_session(engine, system_ctx(tenant)) as s:
        return str(await s.scalar(text("select incident_id from incidents")))


async def test_an_incident_stream_replays_its_feed_and_resumes_after_last_event_id(
    app_engine: AsyncEngine,
) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    incident_id = await _incident(app_engine, tenant)
    with _fast(api_client(app_engine)) as client:
        status, events = _read(
            client, f"/incidents/{incident_id}/stream", _headers(subject, tenant)
        )
        assert status == 200
        assert [e["event"] for e in events] == ["incident.opened", "incident.event_attached"]
        assert all(e["data"]["incident_id"] == incident_id for e in events)
        assert set(events[0]["data"]) == {"incident_id", "type", "ref_id", "at"}
        first = events[0]["id"]
        _, resumed = _read(
            client,
            f"/incidents/{incident_id}/stream",
            _headers(subject, tenant, **{"Last-Event-ID": first}),
        )
    assert [e["id"] for e in resumed] == [e["id"] for e in events[1:]]


async def test_the_tenant_stream_sees_only_its_own_tenant(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    other, _, _ = await _tenant(app_engine)
    mine = await _incident(app_engine, tenant)
    await _incident(app_engine, other)
    with _fast(api_client(app_engine)) as client:
        # Without Last-Event-ID the stream starts now, not at the beginning of time.
        _, fresh = _read(client, "/stream", _headers(subject, tenant))
        _, replay = _read(client, "/stream", _headers(subject, tenant, **{"Last-Event-ID": "0"}))
    assert fresh == []
    assert {e["data"]["incident_id"] for e in replay} == {mine}
    assert [e["event"] for e in replay][:1] == ["incident.opened"]


async def test_another_tenants_incident_is_not_found(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    other, _, _ = await _tenant(app_engine)
    theirs = await _incident(app_engine, other)
    with _fast(api_client(app_engine)) as client:
        status, [body] = _read(client, f"/incidents/{theirs}/stream", _headers(subject, tenant))
        assert (status, body["code"]) == (404, "incident.not_found")
        status, [body] = _read(client, "/stream", _headers(subject, other))
        assert (status, body["code"]) == (404, "tenant.not_found")


async def test_too_many_streams_are_refused(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    with _fast(api_client(app_engine)) as client:
        client.app.state.max_streams = 0
        status, [body] = _read(client, "/stream", _headers(subject, tenant))
    assert (status, body["code"]) == (429, "stream.too_many")


async def test_a_removed_member_is_cut_off_at_the_next_heartbeat(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)

    def remove() -> None:
        async def run() -> None:
            # Its own engine: this runs on another thread's event loop.
            engine = create_engine(app_engine.url, poolclass=NullPool)
            async with tenant_session(engine, system_ctx(tenant)) as s:
                await s.execute(text("delete from memberships"))
            await engine.dispose()

        asyncio.run(run())

    with _fast(api_client(app_engine)) as client:
        threading.Timer(0.3, remove).start()
        started = datetime.now(UTC)
        status, _ = _read(client, "/stream", _headers(subject, tenant, ttl_s=30))
    assert status == 200
    assert datetime.now(UTC) - started < timedelta(seconds=10)
