"""Publishing internal events; delivery is at least once, so every consumer must be idempotent."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError

Handler = Callable[[RequestContext, dict[str, Any]], Awaitable[None]]


class UnknownTopic(OpsteamError):
    code = "event.unknown_topic"


class EventPublisher(Protocol):
    async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None: ...


class InProcessPublisher:
    """Calls the topic's handler in this process; a topic without one is an error, not a drop."""

    def __init__(self, handlers: Mapping[str, Handler]) -> None:
        self._handlers = dict(handlers)

    async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None:
        handler = self._handlers.get(topic)
        if handler is None:
            raise UnknownTopic(f"no handler for topic {topic}")
        await handler(ctx, payload)


class FakePublisher:
    """Records what was published, in order."""

    def __init__(self) -> None:
        self.published: list[tuple[RequestContext, str, dict[str, Any]]] = []

    async def publish(self, ctx: RequestContext, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((ctx, topic, payload))
