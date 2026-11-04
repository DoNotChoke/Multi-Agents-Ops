import pytest
from opsteam_core.budget import BudgetCaps, MemoryBudget
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_llm.client import Message
from opsteam_llm.fake import FakeLLM
from opsteam_llm.gateway import LLMGateway, Models, ModelSpec
from opsteam_worker.recording import llm_recorder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

MODELS = Models(
    tiers={"tier2": ["big"]},
    specs={"big": ModelSpec("fake", "fake-big", usd_in_per_mtok=3.0, usd_out_per_mtok=15.0)},
)


async def test_each_llm_call_is_stored_as_metadata_under_its_run(app_engine: AsyncEngine) -> None:
    tenant, incident, run = new_id("tnt"), new_id("inc"), new_id("run")
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        await s.execute(
            text(
                "insert into incidents (incident_id, tenant_id, source, title, status, severity,"
                " opened_at, detected_at, created_by)"
                " values (:i, :t, 'alert', 'x', 'TRIAGED', 'high', now(), now(), 'system:test')"
            ),
            {"i": incident, "t": tenant},
        )
        await s.execute(
            text(
                "insert into agent_runs (run_id, tenant_id, incident_id, agent, prompt_version,"
                " model_alias) values (:r, :t, :i, 'metrics', 'metrics@1', 'tier2')"
            ),
            {"r": run, "t": tenant, "i": incident},
        )
    gateway = LLMGateway(
        MODELS,
        {"fake": FakeLLM([{"content": "p95 doubled", "tokens_in": 2000, "tokens_out": 100}])},
        budget=MemoryBudget(BudgetCaps()),
        recorder=llm_recorder(app_engine, run),
    )
    ctx = system_ctx(tenant).model_copy(update={"incident_id": incident})
    await gateway.complete(
        ctx, tier="tier2", messages=[Message(role="user", content="why?")], max_output_tokens=200
    )
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        [row] = (await s.execute(text("select * from llm_calls"))).mappings().all()
    assert (row["run_id"], row["model_id"], row["tier"]) == (run, "fake-big", "tier2")
    assert (row["tokens_in"], row["tokens_out"], row["cached"]) == (2000, 100, False)
    assert float(row["cost_usd"]) == pytest.approx(0.0075)
    assert "p95 doubled" not in str(dict(row)) and "why?" not in str(dict(row))
