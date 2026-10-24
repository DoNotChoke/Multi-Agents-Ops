"""What a tool is: its argument schema, who may call it and the limits the Gateway enforces."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


@dataclass(frozen=True)
class Limits:
    """Hard ceilings; the Gateway clamps requests to the tightest of these and the integration's."""

    max_bytes: int
    max_series: int
    max_window_s: int = 0
    min_step_s: int = 0
    max_points: int = 0


@dataclass(frozen=True)
class ToolSpec:
    """A tool the LLM may request; `args` rejects unknown fields."""

    name: str
    description: str
    args: type[BaseModel]
    integration_type: str
    allowed_agents: frozenset[str]
    timeout_ms: int
    limits: Limits
    permission: Literal["READ"] = "READ"
