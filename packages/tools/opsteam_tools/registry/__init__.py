"""Every tool the LLM may request, by name."""

from opsteam_tools.registry.k8s import K8S_EVENTS, K8S_GET_RESOURCES, K8S_ROLLOUTS
from opsteam_tools.registry.logs import LOGS_AGGREGATE, LOGS_GET_CONTEXT, LOGS_SEARCH
from opsteam_tools.registry.metrics import ALERTS_LIST, QUERY_INSTANT, QUERY_RANGE
from opsteam_tools.registry.spec import ToolSpec

TOOLS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        QUERY_RANGE, QUERY_INSTANT, ALERTS_LIST, LOGS_SEARCH, LOGS_AGGREGATE, LOGS_GET_CONTEXT,
        K8S_GET_RESOURCES, K8S_EVENTS, K8S_ROLLOUTS,
    )
}  # fmt: skip
