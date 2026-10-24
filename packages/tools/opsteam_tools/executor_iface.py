"""ToolExecutor Protocol: the only way opsteam_agents can run a tool."""

from typing import Protocol

from opsteam_contracts.tool_request import ToolRequest
from opsteam_contracts.tool_result import ToolResult
from opsteam_core.context import RequestContext


class ToolExecutor(Protocol):
    """Runs one tool request for `agent`; refusals and failures come back as a ToolResult status."""

    async def execute(
        self, ctx: RequestContext, request: ToolRequest, *, agent: str
    ) -> ToolResult: ...
