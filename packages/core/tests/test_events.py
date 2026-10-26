from typing import Any

import pytest
from opsteam_core.context import Principal, RequestContext
from opsteam_core.events import FakePublisher, InProcessPublisher, UnknownTopic

CTX = RequestContext(
    tenant_id="tnt_01JX0000000000000000000000",
    principal=Principal(type="system", id="test"),
    roles=(),
    scopes=frozenset(),
)


async def test_in_process_publisher_calls_the_topic_handler() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(ctx: RequestContext, payload: dict[str, Any]) -> None:
        seen.append((ctx.tenant_id, payload))

    publisher = InProcessPublisher({"event.received": handler})
    await publisher.publish(CTX, "event.received", {"event_id": "evt_1"})
    assert seen == [(CTX.tenant_id, {"event_id": "evt_1"})]


async def test_unknown_topics_are_refused_not_dropped() -> None:
    with pytest.raises(UnknownTopic):
        await InProcessPublisher({}).publish(CTX, "nope", {})


async def test_fake_publisher_records_in_order() -> None:
    fake = FakePublisher()
    await fake.publish(CTX, "a", {"n": 1})
    await fake.publish(CTX, "b", {"n": 2})
    assert fake.published == [(CTX, "a", {"n": 1}), (CTX, "b", {"n": 2})]
