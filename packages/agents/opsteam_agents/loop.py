"""The loop every specialist runs: ask the model, run the tool it asks for, until it concludes."""

import json
from dataclasses import dataclass

from opsteam_contracts.agent_result import AgentResult
from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.budget import BudgetExceeded
from opsteam_core.clock import Clock, SystemClock
from opsteam_core.context import RequestContext
from opsteam_core.ids import new_id
from opsteam_llm.client import LLMClient, LLMResponse, Message, ModelUnavailable, ToolSchema
from opsteam_tools.executor_iface import ToolExecutor
from opsteam_tools.registry import TOOLS

from opsteam_agents.models import (
    AgentSpec,
    AgentStep,
    AgentTask,
    Conclusion,
    Final,
    RunRecorder,
    ToolCall,
)
from opsteam_agents.untrusted import wrap_untrusted

RETRY = "Your answer was rejected: {problem}. Reply with one JSON object that matches the schema."
# The incident's shared tool budget is spent: no later call can succeed, so stop now.
TOOL_BUDGET_SPENT = "quota.incident_budget"


@dataclass
class _Tally:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model_id: str | None = None

    def add(self, response: LLMResponse) -> None:
        self.llm_calls += 1
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        self.cost_usd += response.cost_usd
        self.model_id = response.model_id


async def run_agent(
    ctx: RequestContext,
    spec: AgentSpec,
    task: AgentTask,
    *,
    tools: ToolExecutor,
    llm: LLMClient,
    recorder: RunRecorder,
    clock: Clock = SystemClock(),  # noqa: B008
) -> AgentResult:
    """Run one agent to a conclusion or a limit; every run is recorded, failed ones included."""
    if ctx.incident_id is None:
        raise ValueError("an agent run belongs to an incident")
    ctx = ctx.model_copy(update={"run_id": new_id("run")})
    prompt = spec.prompt
    await recorder.started(
        ctx, agent=prompt.agent, prompt_version=prompt.prompt_version, model_alias=spec.tier
    )
    started, tally = clock.now(), _Tally()
    try:
        status, stop_reason, conclusion = await _steps(ctx, spec, task, tools, llm, clock, tally)
    except BudgetExceeded:
        status, stop_reason, conclusion = "budget_exhausted", "budget", _cut_short("budget")
    except ModelUnavailable:
        status, stop_reason, conclusion = "failed", "error", _cut_short("model unavailable")
    result = AgentResult.model_validate(
        {
            "schema_version": 1,
            "run_id": ctx.run_id,
            "incident_id": ctx.incident_id,
            "agent": prompt.agent,
            "status": status,
            **conclusion.model_dump(mode="json"),
            "budget_used": {
                "llm_calls": tally.llm_calls,
                "tokens_in": tally.tokens_in,
                "tokens_out": tally.tokens_out,
                "tool_calls": tally.tool_calls,
                "cost_usd": round(tally.cost_usd, 6),
                "duration_ms": int((clock.now() - started).total_seconds() * 1000),
            },
            "stop_reason": stop_reason,
        }
    )
    await recorder.finished(ctx, result, tally.model_id)
    return result


async def _steps(
    ctx: RequestContext,
    spec: AgentSpec,
    task: AgentTask,
    tools: ToolExecutor,
    llm: LLMClient,
    clock: Clock,
    tally: _Tally,
) -> tuple[str, str, Conclusion]:
    started = clock.now()
    allowed = spec.prompt.allowed_tools
    schemas = [
        ToolSchema(
            name=n, description=TOOLS[n].description, parameters=TOOLS[n].args.model_json_schema()
        )
        for n in sorted(allowed & TOOLS.keys())
    ]
    messages = [
        Message(role="system", content=spec.prompt.body),
        Message(role="user", content=_brief(task)),
    ]
    evidence: set[str] = set()
    retried = False
    for _ in range(spec.max_steps):
        if (clock.now() - started).total_seconds() >= spec.deadline_s:
            return "partial", "deadline", _cut_short("deadline")
        response = await llm.complete(
            ctx,
            tier=spec.tier,
            messages=messages,
            tools=schemas,
            response_schema=AgentStep,
            max_output_tokens=spec.max_output_tokens,
        )
        tally.add(response)
        messages.append(Message(role="assistant", content=response.content))
        step = response.parsed.root if isinstance(response.parsed, AgentStep) else None
        problem = response.parse_error or ("no answer" if step is None else None)
        if isinstance(step, Final):
            problem = _uncited(step.result, evidence)
        if problem is not None:
            if retried:
                return "failed", "error", _cut_short("the model's answers did not match the schema")
            retried = True
            messages.append(Message(role="user", content=RETRY.format(problem=problem)))
            continue
        retried = False
        if isinstance(step, Final):
            found = step.result.findings or step.result.hypotheses
            return "completed", "done" if found else "no_data", step.result
        if not isinstance(step, ToolCall):  # unreachable: problem was set above
            continue
        answer = await _call(ctx, step.request, spec, tools, tally)
        evidence.update(answer.evidence_ids)
        if answer.error is not None and answer.error.code == TOOL_BUDGET_SPENT:
            return "budget_exhausted", "budget", _cut_short("budget")
        messages.append(Message(role="user", content=_observation(step.request, answer, spec)))
    return "partial", "budget", _cut_short("step limit")


async def _call(
    ctx: RequestContext, request: ToolRequest, spec: AgentSpec, tools: ToolExecutor, tally: _Tally
) -> ToolResult:
    """Refuse tools outside the prompt's list before the Gateway, which checks again."""
    if request.tool not in spec.prompt.allowed_tools:
        return _denied("tool.not_allowed", f"{request.tool} is not one of your tools")
    tally.tool_calls += 1
    return await tools.execute(ctx, request, agent=spec.prompt.agent)


def _brief(task: AgentTask) -> str:
    integrations = ", ".join(f"{kind}={iid}" for kind, iid in sorted(task.integrations.items()))
    return (
        f"Task: {task.goal}\n"
        f"Service: {task.resource.service} in environment {task.resource.environment}\n"
        f"Time window: {task.time_window.start_at.isoformat()} to "
        f"{task.time_window.end_at.isoformat()}\n"
        f"Integrations: {integrations or 'none'}\n"
        'Answer with one JSON object: {"action": "tool_call", "request": <ToolRequest>} or '
        '{"action": "final", "result": {"findings": [...], "hypotheses": [...], '
        '"missing_data": [...]}}.'
    )


def _observation(request: ToolRequest, answer: ToolResult, spec: AgentSpec) -> str:
    """Trusted facts about the call first, then the system's data wrapped as untrusted."""
    error = f"; error={answer.error.code}: {answer.error.message}" if answer.error else ""
    head = (
        f"Result of {request.tool}: status={answer.status}; "
        f"evidence_ids={json.dumps(answer.evidence_ids)}{error}"
    )
    if answer.data is None:
        return head
    payload = {"summary": answer.summary, "data": answer.data.model_dump(mode="json")}
    body = wrap_untrusted(
        request.tool,
        json.dumps(payload),
        tool_call_id=answer.tool_call_id,
        truncated=answer.data.truncated,
        max_chars=spec.max_chars_per_result,
    )
    return f"{head}\n{body}"


def _uncited(conclusion: Conclusion, evidence: set[str]) -> str | None:
    """Every cited evidence id must have come back from a tool in this run."""
    cited = {e for f in conclusion.findings for e in f.evidence_ids}
    for h in conclusion.hypotheses:
        cited.update(h.evidence_ids, h.counterevidence_ids)
    unknown = sorted(cited - evidence)
    return f"evidence_ids no tool returned in this run: {unknown}" if unknown else None


def _cut_short(reason: str) -> Conclusion:
    return Conclusion(missing_data=[f"investigation stopped before a conclusion: {reason}"])


def _denied(code: str, message: str) -> ToolResult:
    return ToolResult.model_validate(
        {
            "schema_version": 1,
            "tool_call_id": new_id("tc"),
            "status": "denied",
            "evidence_ids": [],
            "error": {"code": code, "message": message},
            "usage": {"duration_ms": 0, "bytes": 0, "rows": 0, "series": 0},
        }
    )
