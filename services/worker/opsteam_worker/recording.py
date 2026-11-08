"""Persist agent runs and the LLM calls they make; metadata only, never prompts or answers."""

import json

from opsteam_agents.models import RunRecorder
from opsteam_contracts.agent_result import AgentResult
from opsteam_core.context import RequestContext
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_llm.client import LLMCall
from opsteam_llm.gateway import Recorder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_INSERT_CALL = text("""
    insert into llm_calls (llm_id, tenant_id, run_id, model_id, tier, tokens_in, tokens_out,
                           cached, cost_usd, latency_ms)
    values (:id, :t, :run, :model, :tier, :tin, :tout, :cached, :cost, :latency)
""")


def llm_recorder(engine: AsyncEngine) -> Recorder:
    """Files each call under `ctx.run_id`; calls outside an agent run are not stored."""

    async def record(ctx: RequestContext, call: LLMCall) -> None:
        if ctx.run_id is None:
            return
        async with tenant_session(engine, ctx) as session:
            await session.execute(
                _INSERT_CALL,
                {
                    "id": new_id("llm"),
                    "t": ctx.tenant_id,
                    "run": ctx.run_id,
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


class AgentRunStore(RunRecorder):
    """agent_runs rows: opened when a run starts, closed once with its totals."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def started(
        self, ctx: RequestContext, *, agent: str, prompt_version: str, model_alias: str
    ) -> None:
        async with tenant_session(self.engine, ctx) as session:
            await session.execute(
                text("""
                    insert into agent_runs (run_id, tenant_id, incident_id, agent,
                                            prompt_version, model_alias)
                    values (:run, :t, :incident, :agent, :version, :alias)
                """),
                {
                    "run": ctx.run_id,
                    "t": ctx.tenant_id,
                    "incident": ctx.incident_id,
                    "agent": agent,
                    "version": prompt_version,
                    "alias": model_alias,
                },
            )

    async def finished(
        self, ctx: RequestContext, result: AgentResult, model_id: str | None
    ) -> None:
        used = result.budget_used
        failed = result.status.value == "failed"
        async with tenant_session(self.engine, ctx) as session:
            await session.execute(
                text("""
                    update agent_runs set model_id = :model, status = :status,
                      stop_reason = :stop, tokens_in = :tin, tokens_out = :tout,
                      tool_calls = :tools, cost_usd = :cost, finished_at = now(),
                      error = cast(:error as jsonb)
                    where run_id = :run
                """),
                {
                    "run": result.run_id,
                    "model": model_id,
                    "status": result.status.value,
                    "stop": result.stop_reason.value,
                    "tin": used.tokens_in,
                    "tout": used.tokens_out,
                    "tools": used.tool_calls,
                    "cost": used.cost_usd,
                    "error": json.dumps({"missing_data": result.missing_data}) if failed else None,
                },
            )
