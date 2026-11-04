"""A deterministic provider for dev and CI: replays a script, or obeys injected instructions."""

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from opsteam_llm.client import Message, ModelUnavailable, ProviderReply, ToolSchema

_UNTRUSTED = re.compile(r"<untrusted_data[^>]*>(.*?)</untrusted_data>", re.DOTALL)
_TOOL_JSON = re.compile(r'\{\s*"tool"\s*:.*\}', re.DOTALL)


class FakeLLM:
    """Each call returns the next turn: a dict (content, tokens_in, tokens_out) or an exception.

    `tricked=True` models a model fooled by prompt injection: it proposes whatever tool request
    it finds inside untrusted data, so tests can prove the Gateway still refuses it.
    """

    def __init__(self, turns: Sequence[dict[str, Any] | Exception], *, tricked: bool = False):
        self.turns, self.tricked = list(turns), tricked
        self.calls = 0
        self.seen: list[list[Message]] = []

    async def generate(
        self,
        model_id: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        response_schema: type[BaseModel] | None,
        max_output_tokens: int,
    ) -> ProviderReply:
        self.calls += 1
        self.seen.append(messages)
        if self.tricked and (request := _injected(messages)) is not None:
            return ProviderReply(json.dumps({"action": "tool_call", "request": request}), 1, 1)
        if not self.turns:
            raise ModelUnavailable("fake script exhausted")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return ProviderReply(
            content=turn["content"]
            if isinstance(turn["content"], str)
            else json.dumps(turn["content"]),
            tokens_in=int(turn.get("tokens_in", 1)),
            tokens_out=int(turn.get("tokens_out", 1)),
        )


def _injected(messages: list[Message]) -> dict[str, Any] | None:
    for message in reversed(messages):
        for block in _UNTRUSTED.findall(message.content):
            if (found := _TOOL_JSON.search(block)) is not None:
                try:
                    request = json.loads(found.group(0))
                except ValueError:
                    continue
                if isinstance(request, dict):
                    return request
    return None
