"""LLM Gateway: tier routing with fallback, incident budget, cost, cache, final redaction."""

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from opsteam_core.budget import Budget, budget_key
from opsteam_core.context import RequestContext
from opsteam_core.redact import redact
from pydantic import BaseModel, ValidationError

from opsteam_llm.client import (
    LLMCall,
    LLMResponse,
    Message,
    ModelUnavailable,
    Provider,
    ProviderReply,
    Tier,
    ToolSchema,
)

Recorder = Callable[[RequestContext, LLMCall], Awaitable[None]]
# Input tokens are reserved from a cautious estimate, then settled to the provider's count.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    usd_in_per_mtok: float
    usd_out_per_mtok: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.usd_in_per_mtok + tokens_out * self.usd_out_per_mtok) / 1e6


@dataclass(frozen=True)
class Models:
    """Config: each tier's models in fallback order, and every model's provider and price."""

    tiers: Mapping[str, list[str]]
    specs: Mapping[str, ModelSpec]


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value


class LLMGateway:
    """LLMClient for every agent; a provider never sees secrets or mixes two tenants."""

    def __init__(
        self,
        models: Models,
        providers: Mapping[str, Provider],
        *,
        budget: Budget,
        cache: Cache | None = None,
        recorder: Recorder | None = None,
        retries: int = 2,
        backoff_s: float = 0.5,
    ) -> None:
        self.models, self.providers, self.budget = models, providers, budget
        self.cache, self.recorder = cache, recorder
        self.retries, self.backoff_s = retries, backoff_s

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
    ) -> LLMResponse:
        # Last line of defence: whatever reached the prompt, no secret leaves for the provider.
        messages = [replace_content(m) for m in messages]
        key = None
        if cache_key is not None and self.cache is not None:
            digest = hashlib.sha256(
                json.dumps([m.model_dump() for m in messages]).encode()
            ).hexdigest()
            key = f"t:{ctx.tenant_id}:llm:{tier}:{cache_key}:{digest}"
            if (hit := await self.cache.get(key)) is not None:
                response = _parse(
                    LLMResponse(hit, "cache", 0, 0, 0.0, 0, cached=True), response_schema
                )
                await self._record(ctx, tier, response)
                return response

        account = budget_key(ctx.tenant_id, ctx.incident_id or ctx.request_id)
        estimate_in = math.ceil(sum(len(m.content) for m in messages) / CHARS_PER_TOKEN)
        cheapest = min(self._spec(a).cost(estimate_in, max_output_tokens) for a in self._tier(tier))
        reserved = {
            "llm_calls": 1,
            "tokens_in": estimate_in,
            "tokens_out": max_output_tokens,
            "cost_micro_usd": math.ceil(cheapest * 1e6),
        }
        await self.budget.reserve(account, reserved)
        try:
            spec, reply, latency_ms = await self._route(
                tier, messages, tools, response_schema, max_output_tokens
            )
        except BaseException:
            await self.budget.settle(account, {k: -v for k, v in reserved.items()})
            raise
        cost = spec.cost(reply.tokens_in, reply.tokens_out)
        await self.budget.settle(
            account,
            {
                "tokens_in": reply.tokens_in - estimate_in,
                "tokens_out": reply.tokens_out - max_output_tokens,
                "cost_micro_usd": math.ceil(cost * 1e6) - reserved["cost_micro_usd"],
            },
        )
        response = _parse(
            LLMResponse(
                reply.content, spec.model_id, reply.tokens_in, reply.tokens_out, cost,
                latency_ms, cached=False,
            ),
            response_schema,
        )  # fmt: skip
        if key is not None and self.cache is not None and response.parse_error is None:
            await self.cache.set(key, reply.content)
        await self._record(ctx, tier, response)
        return response

    async def _route(
        self,
        tier: Tier,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        response_schema: type[BaseModel] | None,
        max_output_tokens: int,
    ) -> tuple[ModelSpec, ProviderReply, int]:
        """Try the tier's models in order, each with a few retries; the last failure wins."""
        failure: ModelUnavailable | None = None
        for alias in self._tier(tier):
            spec = self._spec(alias)
            provider = self.providers[spec.provider]
            for attempt in range(self.retries + 1):
                started = time.monotonic()
                try:
                    reply = await provider.generate(
                        spec.model_id, messages, tools, response_schema, max_output_tokens
                    )
                except ModelUnavailable as exc:
                    failure = exc
                    if attempt < self.retries:
                        await asyncio.sleep(self.backoff_s * 2**attempt)
                    continue
                return spec, reply, int((time.monotonic() - started) * 1000)
        raise failure or ModelUnavailable(f"no model configured for {tier}")

    def _tier(self, tier: str) -> list[str]:
        return self.models.tiers.get(tier, [])

    def _spec(self, alias: str) -> ModelSpec:
        return self.models.specs[alias]

    async def _record(self, ctx: RequestContext, tier: Tier, response: LLMResponse) -> None:
        if self.recorder is not None:
            await self.recorder(
                ctx,
                LLMCall(
                    response.model_id, tier, response.tokens_in, response.tokens_out,
                    response.cost_usd, response.latency_ms, response.cached,
                ),
            )  # fmt: skip


def replace_content(message: Message) -> Message:
    return message.model_copy(update={"content": redact(message.content).text})


def _parse(response: LLMResponse, schema: type[BaseModel] | None) -> LLMResponse:
    if schema is None:
        return response
    try:
        return replace(response, parsed=schema.model_validate_json(response.content))
    except ValidationError as exc:
        # Field paths and messages only: the model's text never goes into errors or logs.
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        return replace(response, parse_error=problems)
