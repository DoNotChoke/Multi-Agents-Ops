"""What crosses between the investigation workflow and its activities; plain data, no I/O."""

from typing import Literal

from opsteam_agents.commander import IncidentBrief, InvestigationPlan, PlannedTask
from opsteam_contracts.agent_result import AgentResult
from opsteam_llm.client import Tier
from pydantic import BaseModel, ConfigDict, Field

TASK_QUEUE = "investigation"
WORKFLOW = "IncidentInvestigationWorkflow"
_STRICT = ConfigDict(extra="forbid", frozen=True)


def workflow_id(incident_id: str) -> str:
    return f"inv-{incident_id}"


class InvestigationInput(BaseModel):
    """Only ids: every activity rebuilds and re-checks the context from them."""

    model_config = _STRICT
    tenant_id: str
    incident_id: str
    mode: Literal["alert", "user"] = "alert"
    question: str | None = Field(default=None, max_length=2000)


class Started(BaseModel):
    """The incident as investigating: its brief (None when no service is known) and integrations."""

    model_config = _STRICT
    brief: IncidentBrief | None
    integrations: dict[str, str]


class PlanInput(BaseModel):
    model_config = _STRICT
    input: InvestigationInput
    brief: IncidentBrief
    prior_inconclusive: bool = False


class SpecialistInput(BaseModel):
    model_config = _STRICT
    input: InvestigationInput
    brief: IncidentBrief
    task: PlannedTask
    tier: Tier
    integrations: dict[str, str]


class ConcludeInput(BaseModel):
    model_config = _STRICT
    input: InvestigationInput
    results: list[AgentResult]
    note: str | None = None


class Outcome(BaseModel):
    model_config = _STRICT
    status: Literal["DIAGNOSED", "INCONCLUSIVE"]
    findings: int
    hypotheses: int
    dropped: int


class Status(BaseModel):
    """What the `get_status` query answers."""

    phase: str = "starting"
    round: int = 0
    agents: dict[str, str] = Field(default_factory=dict)
    outcome: Outcome | None = None
    plan: InvestigationPlan | None = None
