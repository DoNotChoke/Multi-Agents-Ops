from pathlib import Path
from typing import Any

import pytest
from opsteam_agents.loop import run_agent
from opsteam_agents.models import AgentTask
from opsteam_agents.specs import SPECIALISTS, specialist
from opsteam_contracts.agent_result import AgentResult
from opsteam_contracts.tool_request import ToolRequest
from opsteam_core.budget import BudgetCaps, MemoryBudget
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_llm.fake import FakeLLM
from opsteam_llm.gateway import LLMGateway, Models, ModelSpec
from opsteam_tools.executors import ReplayToolExecutor
from opsteam_tools.registry import TOOLS

REPLAY = Path(__file__).parents[3] / "evals/replay/checkout-db-pool-after-release/replay.json"
INTEGRATION = new_id("int")
WINDOW = {"start_at": "2026-10-23T04:00:29Z", "end_at": "2026-10-23T04:07:29Z"}
MODELS = Models(tiers={"tier2": ["fake"]}, specs={"fake": ModelSpec("fake", "fake-1", 1.0, 2.0)})
PARAMS = {"service": "checkout", "environment": "shop"}

# Per specialist: a call the replay can answer, one it cannot, and another agent's tool.
CALLS: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
    "metrics": {
        "data": (
            "metrics.query_range",
            {
                "query_template": "http_latency_p95",
                "params": PARAMS,
                "window_seconds": 600,
                "step_seconds": 15,
            },
        ),
        "none": (
            "metrics.query_range",
            {
                "query_template": "cpu_saturation",
                "params": PARAMS,
                "window_seconds": 600,
                "step_seconds": 15,
            },
        ),
        "foreign": ("k8s.events", {"namespace": "shop"}),
    },
    "logs": {
        "data": (
            "logs.search",
            {"selector_template": "error_logs", "params": PARAMS, "window_seconds": 600},
        ),
        "none": (
            "logs.search",
            {
                "selector_template": "error_logs",
                "params": PARAMS,
                "pattern": "certificate expired",
                "window_seconds": 600,
            },
        ),
        "foreign": (
            "metrics.query_instant",
            {"query_template": "http_latency_p95", "params": PARAMS},
        ),
    },
    "infrastructure": {
        "data": ("k8s.events", {"namespace": "shop"}),
        "none": ("k8s.rollouts", {"namespace": "shop", "name": "checkout"}),
        "foreign": (
            "logs.search",
            {"selector_template": "app_logs", "params": PARAMS, "window_seconds": 600},
        ),
    },
}
CATEGORY = {
    "metrics": "resource_exhaustion",
    "logs": "dependency_failure",
    "infrastructure": "deployment_regression",
}


class Recorder:
    def __init__(self) -> None:
        self.versions: list[str] = []

    async def started(self, ctx: RequestContext, **fields: Any) -> None:
        self.versions.append(fields["prompt_version"])

    async def finished(
        self, ctx: RequestContext, result: AgentResult, model_id: str | None
    ) -> None: ...


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=new_id("tnt"),
        principal=Principal(type="system", id="test"),
        roles=(),
        scopes=frozenset(),
        incident_id=new_id("inc"),
    )


def _request(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": tool,
        "integration_id": INTEGRATION,
        "resource": {"service": "checkout", "environment": "shop"},
        "arguments": arguments,
    }


def _call(agent: str, case: str) -> dict[str, Any]:
    tool, arguments = CALLS[agent][case]
    return {"content": {"action": "tool_call", "request": _request(tool, arguments)}}


def _final(agent: str, evidence: str) -> dict[str, Any]:
    result = {
        "findings": [
            {
                "claim": "checkout degraded after the v2 rollout",
                "severity": "high",
                "evidence_ids": [evidence],
                "time_window": WINDOW,
            }
        ],
        "hypotheses": [
            {
                "description": "checkout v2 starves its database pool",
                "category": CATEGORY[agent],
                "entities": ["checkout", "postgres"],
                "evidence_ids": [evidence],
                "counterevidence_ids": [],
            }
        ],
    }
    return {"content": {"action": "final", "result": result}}


ABSTAIN = {"content": {"action": "final", "result": {"missing_data": ["nothing recorded"]}}}


async def _run(agent: str, turns: list[Any]) -> tuple[AgentResult, FakeLLM, Recorder]:
    fake, recorder = FakeLLM(turns), Recorder()
    task = AgentTask.model_validate(
        {
            "goal": "Explain checkout's latency alert",
            "resource": {"service": "checkout", "environment": "shop"},
            "time_window": WINDOW,
            "integrations": {"prometheus": INTEGRATION},
        }
    )
    out = await run_agent(
        _ctx(),
        specialist(agent),
        task,
        tools=ReplayToolExecutor(REPLAY),
        llm=LLMGateway(MODELS, {"fake": fake}, budget=MemoryBudget(BudgetCaps())),
        recorder=recorder,
    )
    return out, fake, recorder


async def _evidence(agent: str) -> str:
    tool, arguments = CALLS[agent]["data"]
    request = ToolRequest.model_validate(_request(tool, arguments))
    answer = await ReplayToolExecutor(REPLAY).execute(_ctx(), request, agent=agent)
    assert answer.status == "ok", answer.error
    (evidence,) = answer.evidence_ids
    return evidence


@pytest.mark.parametrize("agent", SPECIALISTS)
async def test_with_data_the_finding_cites_real_evidence(agent: str) -> None:
    evidence = await _evidence(agent)
    out, fake, recorder = await _run(agent, [_call(agent, "data"), _final(agent, evidence)])
    assert (out.status, out.stop_reason) == ("completed", "done"), out.missing_data
    assert out.findings[0].evidence_ids == [evidence]
    assert out.hypotheses[0].category.value == CATEGORY[agent]
    assert out.hypotheses[0].entities == ["checkout", "postgres"]
    assert recorder.versions == [f"{agent}@1"]
    assert "<untrusted_data" in fake.seen[1][-1].content


@pytest.mark.parametrize("agent", SPECIALISTS)
async def test_without_data_the_agent_abstains(agent: str) -> None:
    out, fake, _ = await _run(agent, [_call(agent, "none"), ABSTAIN])
    assert (out.status, out.stop_reason, out.findings) == ("completed", "no_data", [])
    observation = fake.seen[1][-1].content
    assert "status=" in observation and (
        "replay.missing" in observation or '"lines": 0' in observation
    )


@pytest.mark.parametrize("agent", SPECIALISTS)
async def test_another_agents_tool_is_refused_without_crashing(agent: str) -> None:
    out, fake, _ = await _run(agent, [_call(agent, "foreign"), ABSTAIN])
    assert out.status == "completed" and out.budget_used.tool_calls == 0
    assert "tool.not_allowed" in fake.seen[1][-1].content


@pytest.mark.parametrize("agent", SPECIALISTS)
def test_each_prompt_lists_exactly_the_tools_built_for_its_agent(agent: str) -> None:
    prompt = specialist(agent).prompt
    assert prompt.agent == agent and prompt.prompt_version == f"{agent}@1"
    assert prompt.allowed_tools == {n for n, s in TOOLS.items() if agent in s.allowed_agents}
    assert "<untrusted_data>" in prompt.body and "evidence_ids" in prompt.body


def test_only_specialists_have_specs() -> None:
    with pytest.raises(KeyError):
        specialist("commander")


async def test_replayed_evidence_ids_are_stable() -> None:
    assert await _evidence("logs") == await _evidence("logs") != await _evidence("metrics")
