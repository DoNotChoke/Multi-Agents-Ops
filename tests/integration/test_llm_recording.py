import pytest
from opsteam_agents.loop import run_agent
from opsteam_agents.models import AgentSpec, AgentTask
from opsteam_agents.prompts import Prompt
from opsteam_core.budget import BudgetCaps, MemoryBudget
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_llm.client import Message
from opsteam_llm.fake import FakeLLM
from opsteam_llm.gateway import LLMGateway, Models, ModelSpec
from opsteam_tools.executors import FakeToolExecutor
from opsteam_worker.recording import AgentRunStore, llm_recorder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.api_helpers import system_ctx

pytestmark = pytest.mark.integration

MODELS = Models(
    tiers={"tier2": ["big"]},
    specs={"big": ModelSpec("fake", "fake-big", usd_in_per_mtok=3.0, usd_out_per_mtok=15.0)},
)
PROMPT = Prompt("metrics@1", "metrics", frozenset({"metrics.query_range"}), "AgentResult", "x")
WINDOW = {"start_at": "2026-11-09T08:00:00Z", "end_at": "2026-11-09T09:00:00Z"}


async def _incident(engine: AsyncEngine) -> tuple[str, str]:
    tenant, incident = new_id("tnt"), new_id("inc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
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
    return tenant, incident


async def test_a_run_and_its_llm_calls_are_stored_as_metadata(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine)
    answer = {"action": "final", "result": {"missing_data": ["p95 doubled; no series stored"]}}
    gateway = LLMGateway(
        MODELS,
        {"fake": FakeLLM([{"content": answer, "tokens_in": 2000, "tokens_out": 100}])},
        budget=MemoryBudget(BudgetCaps()),
        recorder=llm_recorder(app_engine),
    )
    task = AgentTask.model_validate(
        {
            "goal": "why is checkout slow?",
            "resource": {"service": "checkout", "environment": "shop"},
            "time_window": WINDOW,
        }
    )
    ctx = system_ctx(tenant).model_copy(update={"incident_id": incident})
    result = await run_agent(
        ctx,
        AgentSpec(PROMPT),
        task,
        tools=FakeToolExecutor(),
        llm=gateway,
        recorder=AgentRunStore(app_engine),
    )
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        [run] = (await s.execute(text("select * from agent_runs"))).mappings().all()
        [call] = (await s.execute(text("select * from llm_calls"))).mappings().all()
    assert (run["run_id"], run["agent"], run["prompt_version"]) == (
        result.run_id,
        "metrics",
        "metrics@1",
    )
    assert (run["status"], run["stop_reason"], run["model_id"]) == (
        "completed",
        "no_data",
        "fake-big",
    )
    assert (run["tokens_in"], run["tokens_out"], run["finished_at"] is not None) == (
        2000,
        100,
        True,
    )
    assert float(run["cost_usd"]) == pytest.approx(0.0075)
    assert (call["run_id"], call["model_id"], call["tier"]) == (result.run_id, "fake-big", "tier2")
    assert (call["tokens_in"], call["tokens_out"], call["cached"]) == (2000, 100, False)
    assert "p95 doubled" not in str(dict(call)) and "checkout slow" not in str(dict(call))


async def test_calls_outside_a_run_are_not_stored(app_engine: AsyncEngine) -> None:
    tenant, incident = await _incident(app_engine)
    gateway = LLMGateway(
        MODELS,
        {"fake": FakeLLM([{"content": "ok"}])},
        budget=MemoryBudget(BudgetCaps()),
        recorder=llm_recorder(app_engine),
    )
    ctx = system_ctx(tenant).model_copy(update={"incident_id": incident})
    await gateway.complete(
        ctx, tier="tier2", messages=[Message(role="user", content="hi")], max_output_tokens=10
    )
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        assert await s.scalar(text("select count(*) from llm_calls")) == 0
