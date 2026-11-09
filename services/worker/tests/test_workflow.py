import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from opsteam_agents.commander import IncidentBrief, InvestigationPlan
from opsteam_contracts.agent_result import AgentResult
from opsteam_core.ids import derived_id
from opsteam_worker.workflows.investigation import IncidentInvestigationWorkflow
from opsteam_worker.workflows.models import (
    TASK_QUEUE,
    ConcludeInput,
    InvestigationInput,
    Outcome,
    PlanInput,
    SpecialistInput,
    Started,
    Status,
    workflow_id,
)
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

HISTORY = Path(__file__).parent / "histories" / "investigation-v1.json"
TENANT, INCIDENT = derived_id("tnt", b"workflow"), derived_id("inc", b"workflow")
WINDOW = {"start_at": "2026-11-10T08:00:00Z", "end_at": "2026-11-10T09:00:00Z"}
BRIEF = IncidentBrief.model_validate(
    {
        "incident_id": INCIDENT,
        "title": "checkout is slow",
        "severity": "high",
        "services": [{"service": "checkout", "environment": "shop"}],
        "signals": [{"name": "HighLatencyP95"}],
        "time_window": WINDOW,
    }
)


def _plan(*agents: str) -> InvestigationPlan:
    return InvestigationPlan.model_validate(
        {
            "tasks": [
                {"agent": a, "goal": f"{a} goal", "resource": BRIEF.services[0].model_dump()}
                for a in agents
            ],
            "rationale": "rules",
            "source": "rules",
            "tier": "tier2",
            "tier_reason": "severity high",
        }
    )


def _result(agent: str) -> AgentResult:
    return AgentResult.model_validate(
        {
            "schema_version": 1,
            "run_id": derived_id("run", agent.encode()),
            "incident_id": INCIDENT,
            "agent": agent,
            "status": "completed",
            "findings": [],
            "hypotheses": [],
            "missing_data": [],
            "budget_used": {
                "llm_calls": 1, "tokens_in": 1, "tokens_out": 1, "tool_calls": 0,
                "cost_usd": 0, "duration_ms": 1,
            },
            "stop_reason": "no_data",
        }
    )  # fmt: skip


class Fakes:
    """Stand-ins for the four activities that record what the workflow asked for."""

    def __init__(
        self,
        brief: IncidentBrief | None = BRIEF,
        failing: str | None = None,
        status: str = "DIAGNOSED",
    ) -> None:
        self.brief, self.failing, self.status = brief, failing, status
        self.plans: list[PlanInput] = []
        self.specialists: list[str] = []
        self.concluded: list[ConcludeInput] = []
        self.running = 0
        self.peak = 0

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name="begin_investigation")
        async def begin(start: InvestigationInput) -> Started:
            return Started(brief=self.brief, integrations={"prometheus": "int_x"})

        @activity.defn(name="plan_investigation")
        async def plan(request: PlanInput) -> InvestigationPlan:
            self.plans.append(request)
            return _plan("metrics", "logs", "infrastructure")

        @activity.defn(name="run_specialist")
        async def specialist(request: SpecialistInput) -> AgentResult:
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.05)
            self.running -= 1
            self.specialists.append(request.task.agent)
            if request.task.agent == self.failing:
                raise ApplicationError("tool backend down", non_retryable=True)
            return _result(request.task.agent)

        @activity.defn(name="conclude_investigation")
        async def conclude(request: ConcludeInput) -> Outcome:
            self.concluded.append(request)
            status = self.status if self.concluded[-1].results else "INCONCLUSIVE"
            return Outcome(status=status, findings=0, hypotheses=1, dropped=0)  # type: ignore[arg-type]

        return [begin, plan, specialist, conclude]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


async def _start(env: WorkflowEnvironment, fakes: Fakes, **extra: Any) -> Any:
    worker = Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentInvestigationWorkflow],
        activities=fakes.activities(),
    )
    start = InvestigationInput(tenant_id=TENANT, incident_id=INCIDENT, **extra)
    return worker, start


async def test_a_full_round_runs_specialists_in_parallel_then_closes_after_a_day(
    env: WorkflowEnvironment,
) -> None:
    fakes = Fakes()
    worker, start = await _start(env, fakes)
    async with worker:
        handle = await env.client.start_workflow(
            IncidentInvestigationWorkflow.run, start, id=workflow_id(INCIDENT),
            task_queue=TASK_QUEUE,
        )  # fmt: skip
        status: Status = await handle.result()
        history = await handle.fetch_history()
    assert status.phase == "closed" and status.round == 1
    assert status.outcome is not None and status.outcome.status == "DIAGNOSED"
    assert status.agents == {
        "metrics": "completed",
        "logs": "completed",
        "infrastructure": "completed",
    }
    assert sorted(fakes.specialists) == ["infrastructure", "logs", "metrics"]
    assert fakes.peak == 3  # all three ran at once
    assert [len(c.results) for c in fakes.concluded] == [3]
    if not HISTORY.exists():  # the replay fixture is recorded once, then kept
        HISTORY.parent.mkdir(exist_ok=True)
        HISTORY.write_text(history.to_json(), encoding="utf-8")


async def test_follow_up_runs_another_round_with_the_question(env: WorkflowEnvironment) -> None:
    fakes = Fakes(status="INCONCLUSIVE")
    worker, start = await _start(env, fakes)
    async with worker:
        handle = await env.client.start_workflow(
            IncidentInvestigationWorkflow.run, start, id=f"inv-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )  # fmt: skip
        await _until(handle, lambda s: s.phase == "waiting")
        await handle.signal(IncidentInvestigationWorkflow.follow_up, "is payment involved?")
        await _until(handle, lambda s: s.round == 2 and s.phase == "waiting")
        await handle.signal(IncidentInvestigationWorkflow.resolved)
        status: Status = await handle.result()
    assert (status.phase, status.round) == ("resolved", 2)
    first, second = fakes.plans
    assert (first.prior_inconclusive, first.input.question) == (False, None)
    assert (second.prior_inconclusive, second.input.question) == (True, "is payment involved?")


async def test_cancel_ends_the_wait(env: WorkflowEnvironment) -> None:
    worker, start = await _start(env, Fakes())
    async with worker:
        handle = await env.client.start_workflow(
            IncidentInvestigationWorkflow.run, start, id=f"inv-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )  # fmt: skip
        await _until(handle, lambda s: s.phase == "waiting")
        await handle.signal(IncidentInvestigationWorkflow.cancel)
        status: Status = await handle.result()
    assert status.phase == "cancelled"


async def test_a_failed_specialist_does_not_sink_the_others(env: WorkflowEnvironment) -> None:
    fakes = Fakes(failing="logs")
    worker, start = await _start(env, fakes)
    async with worker:
        status: Status = await env.client.execute_workflow(
            IncidentInvestigationWorkflow.run, start, id=f"inv-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )  # fmt: skip
    assert status.agents["logs"] == "failed"
    (concluded,) = fakes.concluded
    assert sorted(r.agent.value for r in concluded.results) == ["infrastructure", "metrics"]
    assert concluded.note == "failed: logs"


async def test_no_known_service_concludes_without_planning(env: WorkflowEnvironment) -> None:
    fakes = Fakes(brief=None)
    worker, start = await _start(env, fakes)
    async with worker:
        status: Status = await env.client.execute_workflow(
            IncidentInvestigationWorkflow.run, start, id=f"inv-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )  # fmt: skip
    assert fakes.plans == [] and status.outcome is not None
    assert status.outcome.status == "INCONCLUSIVE"
    assert fakes.concluded[0].note == "no service is known for this incident"


async def test_the_recorded_history_still_replays() -> None:
    history = WorkflowHistory.from_json(workflow_id(INCIDENT), HISTORY.read_text(encoding="utf-8"))
    replayer = Replayer(
        workflows=[IncidentInvestigationWorkflow], data_converter=pydantic_data_converter
    )
    await replayer.replay_workflow(history)


async def _until(handle: Any, ready: Callable[[Status], bool]) -> None:
    for _ in range(200):
        status: Status = await handle.query(IncidentInvestigationWorkflow.get_status)
        if ready(status):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("workflow never reached the expected state")


def test_history_fixture_is_json() -> None:
    assert json.loads(HISTORY.read_text(encoding="utf-8"))["events"]
