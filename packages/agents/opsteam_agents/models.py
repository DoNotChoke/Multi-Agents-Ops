"""What an agent run receives, what the model may answer at each step, and who records the run."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from opsteam_contracts.agent_result import AgentResult, FindingDraft, HypothesisDraft
from opsteam_contracts.common import TimeWindow
from opsteam_contracts.tool_request import Resource, ToolRequest
from opsteam_core.context import RequestContext
from opsteam_llm.client import Tier
from pydantic import BaseModel, ConfigDict, Field, RootModel

from opsteam_agents.prompts import Prompt

_STRICT = ConfigDict(extra="forbid", frozen=True)


class AgentTask(BaseModel):
    """One specialist's assignment; `integrations` maps an integration type to the id to query."""

    model_config = _STRICT
    goal: str = Field(min_length=1, max_length=1000)
    resource: Resource
    time_window: TimeWindow
    integrations: dict[str, str] = Field(default_factory=dict, max_length=10)


class ToolCall(BaseModel):
    model_config = _STRICT
    action: Literal["tool_call"]
    request: ToolRequest


class Conclusion(BaseModel):
    """The model's verdict; an empty `findings` with `missing_data` is a valid abstention."""

    model_config = _STRICT
    findings: list[FindingDraft] = Field(default_factory=list, max_length=20)
    hypotheses: list[HypothesisDraft] = Field(default_factory=list, max_length=10)
    missing_data: list[Annotated[str, Field(max_length=256)]] = Field(
        default_factory=list, max_length=20
    )


class Final(BaseModel):
    model_config = _STRICT
    action: Literal["final"]
    result: Conclusion


class AgentStep(RootModel[Annotated[ToolCall | Final, Field(discriminator="action")]]):
    """The only shapes a model answer may take: one tool request, or the final conclusion."""


@dataclass(frozen=True)
class AgentSpec:
    prompt: Prompt
    tier: Tier = "tier2"
    max_steps: int = 8
    max_output_tokens: int = 2000
    deadline_s: float = 120.0
    max_chars_per_result: int = 8000


class RunRecorder(Protocol):
    """Stores agent_runs; `ctx.run_id` names the run in both calls."""

    async def started(
        self, ctx: RequestContext, *, agent: str, prompt_version: str, model_alias: str
    ) -> None: ...

    async def finished(
        self, ctx: RequestContext, result: AgentResult, model_id: str | None
    ) -> None: ...
