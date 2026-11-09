"""The specialists the workflow can assign, each with its shipped prompt."""

from opsteam_agents.models import AgentSpec
from opsteam_agents.prompts import load_prompt

SPECIALISTS = ("metrics", "logs", "infrastructure")


def specialist(agent: str) -> AgentSpec:
    """The spec for one specialist; raises KeyError for an agent that is not one."""
    if agent not in SPECIALISTS:
        raise KeyError(agent)
    return AgentSpec(load_prompt(agent))
