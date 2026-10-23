from typing import Any

import opsteam_db
import pytest
from opsteam_db import create_engine, tenant_session


def test_public_surface_has_no_raw_session_factory() -> None:
    assert set(opsteam_db.__all__) == {"create_engine", "ping", "tenant_session"}


@pytest.mark.parametrize("ctx", [None, "tnt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T1", {"tenant_id": "x"}])
async def test_tenant_session_requires_request_context(ctx: Any) -> None:
    engine = create_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")
    with pytest.raises(TypeError):
        async with tenant_session(engine, ctx):
            pass
    await engine.dispose()
