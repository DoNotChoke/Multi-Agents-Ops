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
CART = {"service": "cart", "namespace": "shop"}


async def _tenant(engine: AsyncEngine, role: str = "admin") -> tuple[str, str, str]:
    """A tenant with checkout in prod; return tenant, user subject and service id."""
    subject = new_id("usr")
    tenant = await member(engine, subject, [role])
    service_id = new_id("svc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text(
                "insert into services (service_id, tenant_id, name, environment)"
                " values (:i, :t, 'checkout', 'prod')"
            ),
            {"i": service_id, "t": tenant},
        )
    return tenant, subject, service_id


async def _alert(engine: AsyncEngine, tenant: str, labels: dict[str, str], at: datetime) -> None:
    event_id = new_id("evt")
    labels = {"alertname": "HighErrorRate"} | labels
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("""
                insert into events (event_id, tenant_id, source_type, fingerprint, status,
                                    severity_hint, title, labels, annotations, starts_at,
                                    received_at)
                values (:id, :t, 'alertmanager', :fp, 'firing', 'warning', 'errors',
                        cast(:labels as jsonb), '{}', :at, :at)
            """),
            {
                "id": event_id,
                "t": tenant,
                "fp": fingerprint(
                    tenant, "", "alertmanager", "HighErrorRate", labels, collapse=True
                ),
                "labels": json.dumps(labels),
                "at": at,
            },
        )
    await event_consumer(engine)(system_ctx(tenant), {"event_id": event_id})


def _call(engine: AsyncEngine, method: str, path: str, who: tuple[str, str], **kw: Any) -> Any:
    tenant, subject = who
    with api_client(engine) as client:
        return client.request(method, f"/api/v1{path}", headers=auth(subject, tenant), **kw)


async def _incidents(engine: AsyncEngine, tenant: str) -> list[Any]:
    async with tenant_session(engine, system_ctx(tenant)) as s:
        return list(
            (
                await s.execute(
                    text(
                        "select incident_id, service_id, unmapped_service from incidents"
                        " order by opened_at"
                    )
                )
            ).all()
        )


async def test_a_mapping_remaps_the_incident_and_every_later_alert(
    app_engine: AsyncEngine,
) -> None:
    tenant, subject, service_id = await _tenant(app_engine)
    await _alert(app_engine, tenant, CART, T0)
    [unmapped] = await _incidents(app_engine, tenant)
    assert unmapped.service_id is None and unmapped.unmapped_service == {
        "name": "cart",
        "environment": "shop",
    }
    body = {"matchers": CART, "service_id": service_id, "incident_id": unmapped.incident_id}
    resp = _call(app_engine, "POST", "/service-mappings", (tenant, subject), json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["matchers"] == CART
    assert created["service"] == {
        "service_id": service_id,
        "name": "checkout",
        "environment": "prod",
    }
    [remapped] = await _incidents(app_engine, tenant)
    assert (remapped.service_id, remapped.unmapped_service) == (service_id, None)

    # A later alert with the same labels lands on checkout and joins the same incident.
    await _alert(
        app_engine, tenant, CART | {"alertname": "HighLatencyP95"}, T0 + timedelta(minutes=2)
    )
    [same] = await _incidents(app_engine, tenant)
    assert same.incident_id == unmapped.incident_id
    listed = _call(app_engine, "GET", "/service-mappings", (tenant, subject)).json()["items"]
    assert [m["mapping_id"] for m in listed] == [created["mapping_id"]]
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        actions = (await s.scalars(text("select action from audit_events order by seq"))).all()
    assert "service_mapping.created" in actions and "incident.service_mapped" in actions


async def test_delete_stops_the_mapping(app_engine: AsyncEngine) -> None:
    tenant, subject, service_id = await _tenant(app_engine)
    who = (tenant, subject)
    mapping = _call(
        app_engine,
        "POST",
        "/service-mappings",
        who,
        json={"matchers": CART, "service_id": service_id},
    ).json()
    assert (
        _call(app_engine, "DELETE", f"/service-mappings/{mapping['mapping_id']}", who).status_code
        == 204
    )
    await _alert(app_engine, tenant, CART, T0)
    [incident] = await _incidents(app_engine, tenant)
    assert incident.service_id is None
    again = _call(app_engine, "DELETE", f"/service-mappings/{mapping['mapping_id']}", who)
    assert (again.status_code, again.json()["code"]) == (404, "service_mapping.not_found")


@pytest.mark.parametrize(
    "body",
    [
        {"matchers": {}},  # would match every alert
        {"matchers": {f"k{i}": "v" for i in range(11)}},
        {"matchers": {"service": "x" * 300}},
        {"matchers": {"service": "cart"}, "service_id": "checkout"},
        {"matchers": {"service": "cart"}, "tenant_id": "tnt_01OTHER0000000000000000000"},
        {"matchers": {"service": "cart"}, "priority": 10_000},
    ],
)
async def test_invalid_bodies(app_engine: AsyncEngine, body: dict[str, Any]) -> None:
    tenant, subject, service_id = await _tenant(app_engine)
    resp = _call(
        app_engine,
        "POST",
        "/service-mappings",
        (tenant, subject),
        json={"service_id": service_id} | body,
    )
    assert (resp.status_code, resp.json()["code"]) == (422, "request.invalid")


async def test_permissions_and_isolation(app_engine: AsyncEngine) -> None:
    tenant, admin, service_id = await _tenant(app_engine)
    viewer = new_id("usr")
    await member(app_engine, viewer, ["viewer"], tenant)
    body = {"matchers": CART, "service_id": service_id}
    assert (
        _call(app_engine, "POST", "/service-mappings", (tenant, viewer), json=body).status_code
        == 403
    )
    assert _call(app_engine, "GET", "/service-mappings", (tenant, viewer)).status_code == 200
    created = _call(app_engine, "POST", "/service-mappings", (tenant, admin), json=body)
    assert created.status_code == 201
    dup = _call(app_engine, "POST", "/service-mappings", (tenant, admin), json=body)
    assert (dup.status_code, dup.json()["code"]) == (409, "service_mapping.exists")

    other, stranger, other_service = await _tenant(app_engine)
    # Another tenant's service, mapping or incident is simply not there.
    resp = _call(app_engine, "POST", "/service-mappings", (other, stranger), json=body)
    assert (resp.status_code, resp.json()["code"]) == (404, "service.not_found")
    mapping_id = created.json()["mapping_id"]
    resp = _call(app_engine, "DELETE", f"/service-mappings/{mapping_id}", (other, stranger))
    assert resp.status_code == 404
    assert _call(app_engine, "GET", "/service-mappings", (other, stranger)).json()["items"] == []
    await _alert(app_engine, tenant, CART, T0)
    [incident] = await _incidents(app_engine, tenant)
    steal = {"matchers": {"service": "x"}, "service_id": other_service}
    steal["incident_id"] = incident.incident_id
    resp = _call(app_engine, "POST", "/service-mappings", (other, stranger), json=steal)
    assert (resp.status_code, resp.json()["code"]) == (404, "incident.not_found")
