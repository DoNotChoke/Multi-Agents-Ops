import asyncio
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from opsteam_api.routers import streams
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id


async def test_a_client_leaving_lets_the_database_read_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_read(*_: Any) -> list[Any]:
        started.set()
        await asyncio.sleep(0.01)
        finished.set()
        return []

    async def connected() -> bool:
        return False

    monkeypatch.setattr(streams, "_read", slow_read)
    tenant, user = new_id("tnt"), new_id("usr")
    key = (tenant, user)
    state = SimpleNamespace(streams=Counter({key: 1}), engine=None, stream_poll_s=0.01)
    state.stream_heartbeat_s = 60.0
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=state),
        state=SimpleNamespace(token_expires_at=time.time() + 60),
        is_disconnected=connected,
    )
    ctx = RequestContext(
        tenant_id=tenant,
        principal=Principal(type="user", id=user),
        roles=(),
        scopes=frozenset(),
    )

    async def consume() -> None:
        async for _ in streams._events(request, ctx, None, 0, key):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not finished.is_set()  # the response is gone at once...
    await asyncio.wait_for(finished.wait(), 1)  # ...and the query still ran to its end
    assert state.streams[key] == 0
