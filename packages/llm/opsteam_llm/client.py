"""What callers and providers exchange with the LLM Gateway; models are named by tier."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from opsteam_core.context import RequestContext
from opsteam_core.errors import OpsteamError
from pydantic import BaseModel, ConfigDict

Tier = Literal["tier1", "tier2", "tier3"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str


class ToolSchema(BaseModel):
    """A tool the model may propose, described by its JSON Schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str
    parameters: dict[str, Any]


class ModelUnavailable(OpsteamError):
    """A timeout, rate limit or server error: worth a retry or the next model."""

    code = "llm.unavailable"


@dataclass(frozen=True)
class ProviderReply:
    content: str
    tokens_in: int
    tokens_out: int


class Provider(Protocol):
    """One vendor's API behind the gateway; raises ModelUnavailable for transient failures."""

    async def generate(
        self,
        model_id: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        response_schema: type[BaseModel] | None,
        max_output_tokens: int,
    ) -> ProviderReply: ...


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model_id: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    cached: bool
    # Set when response_schema was given: the validated object, or why it did not validate.
    parsed: BaseModel | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class LLMCall:
    """What is kept about a call: metadata only, never the prompt or the answer."""

    model_id: str
    tier: Tier
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    cached: bool


class LLMClient(Protocol):
    async def complete(
        self,
        ctx: RequestContext,
        *,
        tier: Tier,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        response_schema: type[BaseModel] | None = None,
        max_output_tokens: int,
        cache_key: str | None = None,
    ) -> LLMResponse: ...
