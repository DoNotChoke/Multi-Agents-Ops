"""Every tool the LLM may request, by name."""

from opsteam_tools.registry.metrics import ALERTS_LIST, QUERY_INSTANT, QUERY_RANGE
from opsteam_tools.registry.spec import ToolSpec

TOOLS: dict[str, ToolSpec] = {s.name: s for s in (QUERY_RANGE, QUERY_INSTANT, ALERTS_LIST)}
