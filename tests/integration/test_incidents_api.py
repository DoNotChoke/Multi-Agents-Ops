import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_incidents.fingerprint import fingerprint
from opsteam_incidents.manager import event_consumer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import api_client, auth, member, system_ctx

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


async def _tenant(engine: AsyncEngine, role: str = "viewer") -> tuple[str, str, str]:
    """A tenant with a checkout service; return tenant, user subject and service id."""
    subject = new_id("usr")
    tenant = await member(engine, subject, [role])
    service_id = new_id("svc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text(
                "insert into services (service_id, tenant_id, name, environment)"
                " values (:i, :t, 'checkout', 'shop')"
            ),
            {"i": service_id, "t": tenant},
        )
    return tenant, subject, service_id


async def _alert(
    engine: AsyncEngine,
    tenant: str,
    service: str,
    alertname: str,
    at: datetime,
    *,
    severity: str = "warning",
    annotations: dict[str, str] | None = None,
) -> None:
    """Store an event and run it through the Incident Manager, as the relay would."""
    event_id = new_id("evt")
    labels = {"alertname": alertname, "service": service, "namespace": "shop"}
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("""
                insert into events (event_id, tenant_id, source_type, fingerprint, status,
                                    severity_hint, title, labels, annotations, starts_at,
                                    received_at)
                values (:id, :t, 'alertmanager', :fp, 'firing', :sev, :title,
                        cast(:labels as jsonb), cast(:ann as jsonb), :starts, :received)
            """),
            {
                "id": event_id,
                "t": tenant,
                "fp": fingerprint(tenant, "", "alertmanager", alertname, labels, collapse=True),
                "sev": severity,
                "title": f"{alertname} on {service}",
                "labels": json.dumps(labels),
                "ann": json.dumps(annotations or {}),
                "starts": at,
                "received": at + timedelta(seconds=45),
            },
        )
    await event_consumer(engine)(system_ctx(tenant), {"event_id": event_id})


def _get(engine: AsyncEngine, path: str, tenant: str, subject: str, **params: Any) -> Any:
    with api_client(engine) as client:
        return client.get(f"/api/v1{path}", params=params, headers=auth(subject, tenant))


async def test_list_newest_first_with_filters_and_pages(app_engine: AsyncEngine) -> None:
    tenant, subject, service_id = await _tenant(app_engine)
    await _alert(app_engine, tenant, "checkout", "HighLatencyP95", T0)
    await _alert(app_engine, tenant, "cart", "HighErrorRate", T0 + timedelta(hours=1))
    await _alert(
        app_engine, tenant, "auth", "PodCrashLooping", T0 + timedelta(hours=2), severity="critical"
    )
    page = _get(app_engine, "/incidents", tenant, subject).json()
    assert [i["title"] for i in page["items"]] == [
        "PodCrashLooping on auth",
        "HighErrorRate on cart",
        "HighLatencyP95 on checkout",
    ]
    checkout = page["items"][2]
    assert checkout["service"] == {
        "service_id": service_id,
        "name": "checkout",
        "environment": "shop",
    }
    assert page["items"][1]["service"] is None
    assert page["items"][1]["unmapped_service"] == {"name": "cart", "environment": "shop"}
    assert checkout["event_count"] == 1 and page["next_cursor"] is None

    high = _get(app_engine, "/incidents", tenant, subject, severity="high").json()
    assert [i["title"] for i in high["items"]] == ["PodCrashLooping on auth"]
    mine = _get(app_engine, "/incidents", tenant, subject, service_id=service_id).json()
    assert [i["incident_id"] for i in mine["items"]] == [checkout["incident_id"]]
    since = _get(
        app_engine, "/incidents", tenant, subject, since=(T0 + timedelta(minutes=30)).isoformat()
    ).json()
    assert len(since["items"]) == 2

    first = _get(app_engine, "/incidents", tenant, subject, limit=2).json()
    rest = _get(
        app_engine, "/incidents", tenant, subject, limit=2, cursor=first["next_cursor"]
    ).json()
    ids = [i["incident_id"] for i in first["items"] + rest["items"]]
    assert ids == [i["incident_id"] for i in page["items"]] and rest["next_cursor"] is None


async def test_detail_has_timeline_events_and_time_to_detect(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    await _alert(
        app_engine,
        tenant,
        "checkout",
        "HighLatencyP95",
        T0,
        annotations={"summary": "<img src=x onerror=alert(1)>"},
    )
    await _alert(app_engine, tenant, "checkout", "HighErrorRate", T0 + timedelta(minutes=2))
    [summary] = _get(app_engine, "/incidents", tenant, subject).json()["items"]
    resp = _get(app_engine, f"/incidents/{summary['incident_id']}", tenant, subject)
    assert resp.status_code == 200, resp.text
    incident = resp.json()
    assert incident["status"] == "NEW" and incident["event_count"] == 2
    assert incident["started_at"].startswith("2026-10-26T09:00:00")
    assert incident["time_to_detect_seconds"] == 45
    assert [(t["from_status"], t["to_status"]) for t in incident["timeline"]] == [(None, "NEW")]
    assert [e["labels"]["alertname"] for e in incident["events"]] == [
        "HighLatencyP95",
        "HighErrorRate",
    ]
    # Customer text comes back exactly as stored: the web renders it, never the API.
    assert incident["events"][0]["annotations"]["summary"] == "<img src=x onerror=alert(1)>"
    assert incident["events_truncated"] is False


async def test_other_tenants_and_unknown_ids_are_not_found(app_engine: AsyncEngine) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    await _alert(app_engine, tenant, "checkout", "HighLatencyP95", T0)
    [mine] = _get(app_engine, "/incidents", tenant, subject).json()["items"]
    stranger_tenant, stranger, _ = await _tenant(app_engine)
    assert _get(app_engine, "/incidents", stranger_tenant, stranger).json()["items"] == []
    for who in ((stranger_tenant, stranger), (tenant, subject)):
        incident_id = mine["incident_id"] if who[1] == stranger else new_id("inc")
        resp = _get(app_engine, f"/incidents/{incident_id}", *who)
        assert (resp.status_code, resp.json()["code"]) == (404, "incident.not_found")


@pytest.mark.parametrize(
    "params",
    [
        {"status": "DONE"},
        {"severity": "urgent"},
        {"limit": 500},
        {"cursor": "inc_bad"},
        {"service_id": "checkout"},
        {"since": "yesterday"},
    ],
)
async def test_bad_filters_are_422(app_engine: AsyncEngine, params: dict[str, Any]) -> None:
    tenant, subject, _ = await _tenant(app_engine)
    resp = _get(app_engine, "/incidents", tenant, subject, **params)
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")


async def test_requires_a_token(app_engine: AsyncEngine) -> None:
    with api_client(app_engine) as client:
        assert client.get("/api/v1/incidents").status_code == 401
        assert client.get(f"/api/v1/incidents/{new_id('inc')}").status_code == 401
