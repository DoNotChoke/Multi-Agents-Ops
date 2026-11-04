"""Persist what the LLM Gateway reports about each call, under the agent run that made it."""

from opsteam_core.context import RequestContext
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_llm.client import LLMCall
from opsteam_llm.gateway import Recorder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_INSERT = text("""
    insert into llm_calls (llm_id, tenant_id, run_id, model_id, tier, tokens_in, tokens_out,
                           cached, cost_usd, latency_ms)
    values (:id, :t, :run, :model, :tier, :tin, :tout, :cached, :cost, :latency)
""")


def llm_recorder(engine: AsyncEngine, run_id: str) -> Recorder:
    """A recorder for one agent run; it writes metadata only, never prompt or answer."""

    async def record(ctx: RequestContext, call: LLMCall) -> None:
        async with tenant_session(engine, ctx) as session:
            await session.execute(
                _INSERT,
                {
                    "id": new_id("llm"),
                    "t": ctx.tenant_id,
                    "run": run_id,
                    "model": call.model_id,
                    "tier": call.tier,
                    "tin": call.tokens_in,
                    "tout": call.tokens_out,
                    "cached": call.cached,
                    "cost": call.cost_usd,
                    "latency": call.latency_ms,
                },
            )

    return record
