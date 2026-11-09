import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opsteam_contracts.common import TimeWindow
from opsteam_contracts.evidence import SourceType
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.budget import BudgetCaps, RedisBudget, budget_key
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_evidence.objects import FakeObjectStore
from opsteam_evidence.store import Provenance, store
from opsteam_llm.client import Message, ProviderReply, ToolSchema
from opsteam_llm.gateway import LLMGateway, Models, ModelSpec
from opsteam_tools.executors import FakeToolExecutor, result
from opsteam_worker.activities import InvestigationActivities
from opsteam_worker.worker import build_worker
from opsteam_worker.workflows.investigation import IncidentInvestigationWorkflow
from opsteam_worker.workflows.models import TASK_QUEUE, InvestigationInput, Status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from tests.api_helpers import system_ctx
from tests.integration.test_gateway import REDIS_URL

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
MODELS = Models(tiers={"tier2": ["fake"]}, specs={"fake": ModelSpec("fake", "fake-1", 1.0, 1.0)})
AGENTS = {"Metrics": "metrics", "Logs": "logs", "Infrastructure": "infrastructure"}


class Scripted:
    """A provider that answers each agent from its own script, so parallel runs never mix."""

    def __init__(self, evidence: str) -> None:
        self.evidence = evidence
        self.turns: dict[str, int] = {}

    async def generate(
        self,
        model_id: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        response_schema: type[BaseModel] | None,
        max_output_tokens: int,
    ) -> ProviderReply:
        system = messages[0].content
        agent = next((a for k, a in AGENTS.items() if f"the {k} specialist" in system), "commander")
        turn = self.turns.get(agent, 0)
        self.turns[agent] = turn + 1
        return ProviderReply(json.dumps(self._answer(agent, turn)), 10, 10)

    def _answer(self, agent: str, turn: int) -> dict[str, Any]:
        if agent == "commander":
            return {"not": "a plan"}  # the rules plan is kept
        if turn == 0:
            tool = {"metrics": "metrics.query_range", "logs": "logs.search"}.get(
                agent, "k8s.events"
            )
            request = {
                "tool": tool,
                "integration_id": new_id("int"),
                "resource": {"service": "checkout", "environment": "shop"},
                "arguments": {},
            }
            return {"action": "tool_call", "request": request}
        window = {"start_at": (NOW - timedelta(hours=1)).isoformat(), "end_at": NOW.isoformat()}
        return {
            "action": "final",
            "result": {
                "findings": [
                    {
                        "claim": f"{agent}: checkout degraded",
                        "severity": "high",
                        "evidence_ids": [self.evidence],
                        "time_window": window,
                    }
                ],
                "hypotheses": [
                    {
                        "description": f"{agent}: release regression",
                        "category": "deployment_regression",
                        "entities": ["checkout"],
                        "evidence_ids": [self.evidence],
                        "counterevidence_ids": [],
                    }
                ],
            },
        }


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


async def _incident(engine: AsyncEngine) -> tuple[str, str, str]:
    """A TRIAGED checkout incident and one real evidence row of the same tenant."""
    tenant, incident, service, call = new_id("tnt"), new_id("inc"), new_id("svc"), new_id("tc")
    async with tenant_session(engine, system_ctx(tenant)) as s:
        await s.execute(
            text("insert into tenants (tenant_id, name) values (:t, 'x')"), {"t": tenant}
        )
        await s.execute(
            text(
                "insert into services (service_id, tenant_id, name, environment)"
                " values (:s, :t, 'checkout', 'shop')"
            ),
            {"s": service, "t": tenant},
        )
        await s.execute(
            text(
                "insert into incidents (incident_id, tenant_id, service_id, source, title, status,"
                " severity, opened_at, detected_at, created_by) values (:i, :t, :s, 'alert',"
                " 'HighLatencyP95 checkout', 'TRIAGED', 'high', now(), now(), 'system:test')"
            ),
            {"i": incident, "t": tenant, "s": service},
        )
        event = new_id("evt")
        await s.execute(
            text("""
                insert into events (event_id, tenant_id, source_type, fingerprint, status,
                                    service_ref, title, labels, annotations, starts_at,
                                    received_at)
                values (:e, :t, 'alertmanager', :fp, 'firing',
                        '{"name": "checkout", "environment": "shop"}', 'HighLatencyP95',
                        '{"alertname": "HighLatencyP95", "service": "checkout"}', '{}', now(),
                        now())
            """),
            {"e": event, "t": tenant, "fp": "a" * 64},
        )
        await s.execute(
            text(
                "insert into incident_events (incident_id, event_id, tenant_id) values (:i, :e, :t)"
            ),
            {"i": incident, "e": event, "t": tenant},
        )
        await s.execute(
            text(
                "insert into tool_calls (tool_call_id, tenant_id, tool, request, status)"
                " values (:c, :t, 'metrics.query_range', '{}', 'ok')"
            ),
            {"c": call, "t": tenant},
        )
        evidence = await store(
            s,
            FakeObjectStore(),
            system_ctx(tenant),
            {"series": []},
            Provenance(
                tool_call_id=call,
                tool="metrics.query_range",
                source_type=SourceType.metrics,
                integration_id=None,
                incident_id=incident,
                source_query={},
                time_window=TimeWindow(start_at=NOW - timedelta(hours=1), end_at=NOW),
                summary="p95 jumped",
                truncated=False,
            ),
        )
    return tenant, incident, evidence.evidence_id


def _tools(evidence: str) -> FakeToolExecutor:
    ok = result("ok", data={"truncated": False}).model_copy(update={"evidence_ids": [evidence]})
    return _Permissive({t: ok for t in ("metrics.query_range", "logs.search", "k8s.events")})


class _Permissive(FakeToolExecutor):
    """Scripted answers without the argument checks; the Gateway's checks have their own tests."""

    async def execute(self, ctx: Any, request: Any, *, agent: str) -> ToolResult:
        self.calls.append((ctx, request, agent))
        return self.script[request.tool]


async def _investigate(
    env: WorkflowEnvironment, engine: AsyncEngine, llm: LLMGateway, tools: FakeToolExecutor,
    tenant: str, incident: str,
) -> Status:  # fmt: skip
    activities = InvestigationActivities(engine, llm=llm, tools=tools)
    async with build_worker(env.client, activities):
        status: Status = await env.client.execute_workflow(
            IncidentInvestigationWorkflow.run,
            InvestigationInput(tenant_id=tenant, incident_id=incident),
            id=f"inv-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    return status


async def test_an_alert_incident_is_investigated_to_diagnosed(
    app_engine: AsyncEngine, redis: Redis, env: WorkflowEnvironment
) -> None:
    tenant, incident, evidence = await _incident(app_engine)
    llm = LLMGateway(MODELS, {"fake": Scripted(evidence)}, budget=RedisBudget(redis, BudgetCaps()))
    status = await _investigate(env, app_engine, llm, _tools(evidence), tenant, incident)
    assert status.outcome is not None and status.outcome.status == "DIAGNOSED"
    assert (status.outcome.findings, status.outcome.hypotheses) == (2, 2)
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        moves = (
            await s.execute(
                text(
                    "select from_status, to_status from incident_transitions"
                    " order by at, transition_id"
                )
            )
        ).all()
        runs = (await s.execute(text("select agent, status from agent_runs order by agent"))).all()
        support = await s.scalar(
            text("select count(*) from hypothesis_evidence where kind = 'support'")
        )
        counter = await s.scalar(
            text("select count(*) from hypothesis_evidence where kind = 'counter'")
        )
        linked = await s.scalar(text("select count(*) from finding_evidence"))
        row = (
            await s.execute(text("select status, summary, first_hypothesis_at from incidents"))
        ).one()
    assert [tuple(m) for m in moves] == [
        ("TRIAGED", "INVESTIGATING"),
        ("INVESTIGATING", "DIAGNOSED"),
    ]
    assert [tuple(r) for r in runs] == [
        ("commander", "failed"),  # its answer was unusable, so the rules plan ran
        ("logs", "completed"),
        ("metrics", "completed"),
    ]
    assert (support, counter, linked) == (2, 0, 2)
    assert row.status == "DIAGNOSED" and row.first_hypothesis_at is not None
    assert row.summary.startswith("2 evidence-backed hypotheses")


async def test_parallel_specialists_never_overspend_the_shared_budget(
    app_engine: AsyncEngine, redis: Redis, env: WorkflowEnvironment
) -> None:
    tenant, incident, evidence = await _incident(app_engine)
    caps = BudgetCaps(llm_calls=4)  # the commander and two specialists each need two calls
    budget = RedisBudget(redis, caps)
    llm = LLMGateway(MODELS, {"fake": Scripted(evidence)}, budget=budget)
    status = await _investigate(env, app_engine, llm, _tools(evidence), tenant, incident)
    used = await budget.used(budget_key(tenant, incident))
    assert used["llm_calls"] <= caps.llm_calls
    assert "budget_exhausted" in status.agents.values()
    async with tenant_session(app_engine, system_ctx(tenant)) as s:
        calls = await s.scalar(text("select count(*) from llm_calls"))
    assert calls <= caps.llm_calls


async def test_another_tenants_incident_is_refused(
    app_engine: AsyncEngine, redis: Redis, env: WorkflowEnvironment
) -> None:
    _, incident, evidence = await _incident(app_engine)
    other, _, _ = await _incident(app_engine)
    llm = LLMGateway(MODELS, {"fake": Scripted(evidence)}, budget=RedisBudget(redis, BudgetCaps()))
    with pytest.raises(Exception, match="Workflow execution failed"):
        await _investigate(env, app_engine, llm, _tools(evidence), other, incident)
