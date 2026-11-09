"""Plan an investigation: rules pick specialists from symptoms, the Commander may refine them."""

import json
from typing import Annotated, Literal

from opsteam_contracts.agent_result import AgentResult
from opsteam_contracts.common import Severity, TimeWindow
from opsteam_contracts.tool_request import Resource
from opsteam_core.budget import BudgetExceeded
from opsteam_core.context import RequestContext
from opsteam_core.ids import new_id
from opsteam_llm.client import LLMClient, LLMResponse, Message, ModelUnavailable, Tier
from pydantic import BaseModel, ConfigDict, Field

from opsteam_agents.models import RunRecorder
from opsteam_agents.prompts import load_prompt
from opsteam_agents.untrusted import wrap_untrusted

Specialist = Literal["metrics", "logs", "infrastructure"]
MAX_AGENTS_PER_ROUND = 3
_STRICT = ConfigDict(extra="forbid", frozen=True)
Text = Annotated[str, Field(min_length=1, max_length=1000)]

# Symptom keywords (matched lower-case in alert names and reasons) and agents, best first.
SYMPTOMS: tuple[tuple[str, tuple[str, ...], tuple[Specialist, ...]], ...] = (
    ("crash", ("oom", "crashloop", "restart"), ("infrastructure", "metrics", "logs")),
    ("rollout", ("rollout", "imagepull", "progressdeadline"), ("infrastructure",)),
    ("node", ("notready", "failedscheduling", "nodepressure"), ("infrastructure", "metrics")),
    ("latency_errors", ("latency", "error", "5xx", "slow", "timeout"), ("metrics", "logs")),
)
DEFAULT_AGENTS: tuple[Specialist, ...] = ("metrics", "logs", "infrastructure")


class Signal(BaseModel):
    """One alert or event attached to the incident; its text comes from the customer's systems."""

    model_config = _STRICT
    name: Annotated[str, Field(max_length=200)]
    reason: Annotated[str, Field(max_length=200)] | None = None


class IncidentBrief(BaseModel):
    """What T0 already knows about an incident; the input of planning and tier choice."""

    model_config = _STRICT
    incident_id: str
    title: Annotated[str, Field(max_length=200)]
    severity: Severity
    services: Annotated[list[Resource], Field(min_length=1, max_length=10)]
    signals: Annotated[list[Signal], Field(max_length=50)] = []
    time_window: TimeWindow
    question: Annotated[str, Field(max_length=2000)] | None = None


class PlannedTask(BaseModel):
    model_config = _STRICT
    agent: Specialist
    goal: Text
    resource: Resource


class InvestigationPlan(BaseModel):
    model_config = _STRICT
    tasks: Annotated[list[PlannedTask], Field(min_length=1, max_length=MAX_AGENTS_PER_ROUND)]
    hypothesized_areas: list[Annotated[str, Field(max_length=200)]] = []
    rationale: Text
    source: Literal["rules", "commander"]
    tier: Tier
    tier_reason: str


class _DraftTask(BaseModel):
    model_config = _STRICT
    agent: Specialist
    goal: Text
    service: Annotated[str, Field(max_length=256)]


class CommanderPlan(BaseModel):
    """What the Commander model may answer; code adds resources, window and tier."""

    model_config = _STRICT
    tasks: Annotated[list[_DraftTask], Field(min_length=1, max_length=MAX_AGENTS_PER_ROUND)]
    hypothesized_areas: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=5)]
    rationale: Text


def symptoms(brief: IncidentBrief) -> list[str]:
    """Names of the known symptoms the brief's signals show, in rule order."""
    text = " ".join(f"{s.name} {s.reason or ''}" for s in brief.signals).lower()
    return [name for name, words, _ in SYMPTOMS if any(w in text for w in words)]


def rule_agents(brief: IncidentBrief) -> list[Specialist]:
    """Specialists for the brief's symptoms, best first, at most MAX_AGENTS_PER_ROUND."""
    found = symptoms(brief)
    ordered = [a for name, _, agents in SYMPTOMS if name in found for a in agents] or list(
        DEFAULT_AGENTS
    )
    return list(dict.fromkeys(ordered))[:MAX_AGENTS_PER_ROUND]


def choose_tier(
    brief: IncidentBrief, *, prior_inconclusive: bool = False, for_verifier: bool = False
) -> tuple[Tier, str]:
    """The model tier an investigation step may use, and why; no LLM decides its own budget."""
    severe = brief.severity.value in ("high", "critical")
    if for_verifier:
        if brief.severity.value == "critical":
            return "tier3", "verifier on a critical incident"
        return "tier2", "verifier"
    if prior_inconclusive:
        return "tier2", "the previous round was inconclusive"
    if len(brief.services) > 1:
        return "tier2", f"{len(brief.services)} services involved"
    if severe:
        return "tier2", f"severity {brief.severity.value}"
    if len(brief.signals) == 1 and symptoms(brief):
        return "tier1", "one service, one known symptom, severity at most medium"
    return "tier2", "symptoms are not a single known pattern"


def rule_plan(brief: IncidentBrief, tier: Tier, tier_reason: str) -> InvestigationPlan:
    """The plan the rules alone produce; also the fallback when the Commander's plan is unusable."""
    found = symptoms(brief) or ["unknown"]
    resource = brief.services[0]
    return InvestigationPlan(
        tasks=[
            PlannedTask(agent=a, goal=GOALS[a].format(service=resource.service), resource=resource)
            for a in rule_agents(brief)
        ],
        hypothesized_areas=found,
        rationale=f"Chosen by symptom rules: {', '.join(found)}.",
        source="rules",
        tier=tier,
        tier_reason=tier_reason,
    )


GOALS: dict[str, str] = {
    "metrics": "Find when {service}'s latency, errors, traffic or saturation left normal.",
    "logs": "Find errors or exceptions in {service} that are new or rising around the incident.",
    "infrastructure": "Find rollouts, restarts, OOM kills or scheduling problems around {service}.",
}


async def plan_investigation(
    ctx: RequestContext,
    brief: IncidentBrief,
    *,
    llm: LLMClient,
    recorder: RunRecorder,
    prior_inconclusive: bool = False,
) -> InvestigationPlan:
    """Rules first, then one Commander call to refine them; any unusable answer keeps the rules."""
    tier, tier_reason = choose_tier(brief, prior_inconclusive=prior_inconclusive)
    fallback = rule_plan(brief, tier, tier_reason)
    prompt = load_prompt("commander")
    ctx = ctx.model_copy(update={"run_id": new_id("run")})
    await recorder.started(
        ctx, agent="commander", prompt_version=prompt.prompt_version, model_alias=tier
    )
    plan, status, stop = fallback, "failed", "error"
    response: LLMResponse | None = None
    try:
        response = await llm.complete(
            ctx,
            tier=tier,
            messages=[
                Message(role="system", content=prompt.body),
                Message(role="user", content=_brief(brief, fallback)),
            ],
            response_schema=CommanderPlan,
            max_output_tokens=1000,
        )
        if isinstance(response.parsed, CommanderPlan):
            refined = _adopt(response.parsed, brief, tier, tier_reason)
            if refined is not None:
                plan, status, stop = refined, "completed", "done"
    except BudgetExceeded:
        status, stop = "budget_exhausted", "budget"
    except ModelUnavailable:
        pass
    model_id = response.model_id if response else None
    await recorder.finished(ctx, _record(ctx, status, stop, response), model_id)
    return plan


def _adopt(
    draft: CommanderPlan, brief: IncidentBrief, tier: Tier, tier_reason: str
) -> InvestigationPlan | None:
    """The Commander's plan if it names only the incident's services and each agent once."""
    services = {s.service: s for s in brief.services}
    agents = [t.agent for t in draft.tasks]
    if len(set(agents)) != len(agents) or any(t.service not in services for t in draft.tasks):
        return None
    return InvestigationPlan(
        tasks=[
            PlannedTask(agent=t.agent, goal=t.goal, resource=services[t.service])
            for t in draft.tasks
        ],
        hypothesized_areas=draft.hypothesized_areas,
        rationale=draft.rationale,
        source="commander",
        tier=tier,
        tier_reason=tier_reason,
    )


def _brief(brief: IncidentBrief, fallback: InvestigationPlan) -> str:
    """Trusted facts in the clear; the title, signals and any user question wrapped as untrusted."""
    services = ", ".join(f"{s.service} ({s.environment})" for s in brief.services)
    observed = {
        "title": brief.title,
        "signals": [s.model_dump(exclude_none=True) for s in brief.signals],
    }
    parts = [
        f"Incident {brief.incident_id}, severity {brief.severity.value}.",
        f"Services: {services}.",
        f"Time window: {brief.time_window.start_at.isoformat()} to "
        f"{brief.time_window.end_at.isoformat()}.",
        f"The symptom rules suggest: {', '.join(t.agent for t in fallback.tasks)}.",
        wrap_untrusted("incident", json.dumps(observed)),
    ]
    if brief.question is not None:
        parts.append(
            "<untrusted_user_input>\n"
            + brief.question.replace("</untrusted_user_input", "&lt;/untrusted_user_input")
            + "\n</untrusted_user_input>"
        )
    parts.append(
        'Answer with one JSON object: {"tasks": [{"agent": ..., "goal": ..., "service": ...}], '
        '"hypothesized_areas": [...], "rationale": ...}.'
    )
    return "\n".join(parts)


def _record(
    ctx: RequestContext, status: str, stop: str, response: LLMResponse | None
) -> AgentResult:
    return AgentResult.model_validate(
        {
            "schema_version": 1,
            "run_id": ctx.run_id,
            "incident_id": ctx.incident_id,
            "agent": "commander",
            "status": status,
            "findings": [],
            "hypotheses": [],
            "missing_data": []
            if status == "completed"
            else ["commander plan unusable; rules used"],
            "budget_used": {
                "llm_calls": 1 if response else 0,
                "tokens_in": response.tokens_in if response else 0,
                "tokens_out": response.tokens_out if response else 0,
                "tool_calls": 0,
                "cost_usd": round(response.cost_usd, 6) if response else 0.0,
                "duration_ms": 0,
            },
            "stop_reason": stop,
        }
    )
