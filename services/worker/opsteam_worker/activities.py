"""The investigation workflow's activities: every database read and write, LLM and tool call."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any, Literal

from opsteam_agents.commander import IncidentBrief, InvestigationPlan, plan_investigation
from opsteam_agents.loop import run_agent
from opsteam_agents.models import AgentTask
from opsteam_agents.specs import specialist
from opsteam_contracts.agent_result import AgentResult
from opsteam_core.clock import Clock, SystemClock
from opsteam_core.context import Principal, RequestContext
from opsteam_core.ids import new_id
from opsteam_db import tenant_session
from opsteam_incidents.state import transition
from opsteam_llm.client import LLMClient
from opsteam_tools.executor_iface import ToolExecutor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from opsteam_worker.recording import AgentRunStore
from opsteam_worker.workflows.models import (
    ConcludeInput,
    InvestigationInput,
    Outcome,
    PlanInput,
    SpecialistInput,
    Started,
)

# How far before the incident opened the specialists look, to see the baseline.
LOOKBACK = timedelta(minutes=30)
INVESTIGATED_TYPES = ("prometheus", "loki", "kubernetes")


class InvestigationActivities:
    """Activities bound to one worker's engine, LLM client and tool executor."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        llm: LLMClient,
        tools: ToolExecutor,
        clock: Clock = SystemClock(),  # noqa: B008
    ) -> None:
        self.engine, self.llm, self.tools, self.clock = engine, llm, tools, clock
        self.runs = AgentRunStore(engine)

    @activity.defn(name="begin_investigation")
    async def begin(self, start: InvestigationInput) -> Started:
        """Move the incident to INVESTIGATING and gather what the plan needs."""
        ctx = _ctx(start)
        async with tenant_session(self.engine, ctx) as session:
            row = await _incident(session, start.incident_id)
            if row.status != "INVESTIGATING":  # a retried activity may find it already moved
                await transition(
                    session, ctx, start.incident_id, "INVESTIGATING",
                    f"investigation started ({start.mode})", self.clock.now(),
                )  # fmt: skip
            services = await _services(session, start.incident_id, row)
            signals = (
                await session.execute(
                    text("""
                        select e.labels->>'alertname' as name,
                               coalesce(e.labels->>'reason', e.annotations->>'summary') as reason
                        from incident_events ie join events e using (event_id)
                        where ie.incident_id = :id order by e.received_at limit 50
                    """),
                    {"id": start.incident_id},
                )
            ).all()
            integrations = {
                kind: iid
                for kind, iid in reversed(
                    (
                        await session.execute(
                            text(
                                "select type, integration_id from integrations"
                                " where status = 'active' and type = any(:types)"
                                " order by created_at"
                            ),
                            {"types": list(INVESTIGATED_TYPES)},
                        )
                    ).all()
                )
            }
        if not services:
            return Started(brief=None, integrations=integrations)
        brief = IncidentBrief.model_validate(
            {
                "incident_id": start.incident_id,
                "title": row.title,
                "severity": row.severity,
                "services": services[:10],
                "signals": [
                    {
                        "name": (s.name or "unnamed")[:200],
                        "reason": (s.reason or None) and s.reason[:200],
                    }
                    for s in signals
                ],
                "time_window": {"start_at": row.opened_at - LOOKBACK, "end_at": self.clock.now()},
                "question": start.question,
            }
        )
        return Started(brief=brief, integrations=integrations)

    @activity.defn(name="plan_investigation")
    async def plan(self, request: PlanInput) -> InvestigationPlan:
        return await plan_investigation(
            _ctx(request.input),
            request.brief,
            llm=self.llm,
            recorder=self.runs,
            prior_inconclusive=request.prior_inconclusive,
        )

    @activity.defn(name="run_specialist")
    async def specialist(self, request: SpecialistInput) -> AgentResult:
        task = AgentTask(
            goal=request.task.goal,
            resource=request.task.resource,
            time_window=request.brief.time_window,
            integrations=request.integrations,
        )
        spec = replace(specialist(request.task.agent), tier=request.tier)
        return await run_agent(
            _ctx(request.input), spec, task, tools=self.tools, llm=self.llm, recorder=self.runs,
            clock=self.clock,
        )  # fmt: skip

    @activity.defn(name="conclude_investigation")
    async def conclude(self, request: ConcludeInput) -> Outcome:
        """Keep claims backed by this tenant's evidence, then DIAGNOSED if any hypothesis stands."""
        ctx = _ctx(request.input)
        incident = request.input.incident_id
        async with tenant_session(self.engine, ctx) as session:
            row = await _incident(session, incident)
            if row.status != "INVESTIGATING":  # already concluded by an earlier attempt
                return Outcome(
                    status="DIAGNOSED" if row.status == "DIAGNOSED" else "INCONCLUSIVE",
                    findings=0, hypotheses=0, dropped=0,
                )  # fmt: skip
            cited = sorted(
                {e for r in request.results for f in r.findings for e in f.evidence_ids}
                | {
                    e
                    for r in request.results
                    for h in r.hypotheses
                    for e in (*h.evidence_ids, *h.counterevidence_ids)
                }
            )
            known = set(
                (
                    await session.scalars(
                        text("select evidence_id from evidence where evidence_id = any(:ids)"),
                        {"ids": cited},
                    )
                ).all()
            )
            findings, hypotheses, dropped, leads = 0, 0, 0, []
            for result in request.results:
                for f in result.findings:
                    evidence = [e for e in f.evidence_ids if e in known]
                    if not evidence:
                        dropped += 1
                        continue
                    await _finding(
                        session, ctx, incident, result, f.model_dump(mode="json"), evidence
                    )
                    findings += 1
                for h in result.hypotheses:
                    support = [e for e in h.evidence_ids if e in known]
                    if not support:
                        dropped += 1
                        continue
                    counter = [e for e in h.counterevidence_ids if e in known]
                    hypotheses += 1
                    await _hypothesis(
                        session, ctx, incident, result, h.model_dump(mode="json"), support,
                        counter, hypotheses,
                    )  # fmt: skip
                    leads.append(h.description)
            missing = sorted({m for r in request.results for m in r.missing_data})
            target: Literal["DIAGNOSED", "INCONCLUSIVE"] = (
                "DIAGNOSED" if hypotheses else "INCONCLUSIVE"
            )
            summary = _summary(target, leads, missing, request.note)
            await session.execute(
                text("""
                    update incidents set summary = :summary,
                      first_hypothesis_at = case when :found
                                                 then coalesce(first_hypothesis_at, now())
                                                 else first_hypothesis_at end
                    where incident_id = :id
                """),
                {"summary": summary, "found": hypotheses > 0, "id": incident},
            )
            await transition(session, ctx, incident, target, summary[:500], self.clock.now())
        return Outcome(status=target, findings=findings, hypotheses=hypotheses, dropped=dropped)


def _ctx(start: InvestigationInput) -> RequestContext:
    """Rebuilt from ids alone; tenant_session's RLS then confines every read to that tenant."""
    return RequestContext(
        tenant_id=start.tenant_id,
        principal=Principal(type="system", id="investigation"),
        roles=(),
        scopes=frozenset(),
        incident_id=start.incident_id,
    )


async def _incident(session: AsyncSession, incident_id: str) -> Any:
    row = (
        await session.execute(
            text(
                "select status, title, severity, opened_at, service_id, unmapped_service"
                " from incidents where incident_id = :id"
            ),
            {"id": incident_id},
        )
    ).one_or_none()
    if row is None:  # unknown id, or one that belongs to another tenant
        raise ApplicationError("incident not found", type="IncidentNotFound", non_retryable=True)
    return row


async def _services(session: AsyncSession, incident_id: str, row: Any) -> list[dict[str, str]]:
    """The incident's own service first, then services of the events folded into it."""
    found: list[dict[str, str]] = []
    if row.service_id is not None:
        own = (
            await session.execute(
                text("select name, environment from services where service_id = :s"),
                {"s": row.service_id},
            )
        ).one_or_none()
        if own is not None:
            found.append({"service": own.name, "environment": own.environment})
    elif row.unmapped_service and row.unmapped_service.get("environment"):
        raw = row.unmapped_service
        found.append({"service": raw["name"], "environment": raw["environment"]})
    others = await session.execute(
        text("""
            select distinct e.service_ref->>'name' as name,
                            e.service_ref->>'environment' as environment
            from incident_events ie join events e using (event_id)
            where ie.incident_id = :id and e.service_ref ? 'name' and e.service_ref ? 'environment'
        """),
        {"id": incident_id},
    )
    for name, environment in others:
        service = {"service": name, "environment": environment}
        if service not in found:
            found.append(service)
    return found


async def _finding(
    session: AsyncSession,
    ctx: RequestContext,
    incident: str,
    result: AgentResult,
    finding: dict[str, Any],
    evidence: list[str],
) -> None:
    finding_id = new_id("fnd")
    await session.execute(
        text("""
            insert into findings (finding_id, tenant_id, incident_id, run_id, agent, claim,
                                  severity, time_window)
            values (:id, :t, :incident, :run, :agent, :claim, :severity, cast(:window as jsonb))
        """),
        {
            "id": finding_id,
            "t": ctx.tenant_id,
            "incident": incident,
            "run": result.run_id,
            "agent": result.agent.value,
            "claim": finding["claim"],
            "severity": finding["severity"],
            "window": json.dumps(finding["time_window"]),
        },
    )
    for evidence_id in evidence:
        await session.execute(
            text(
                "insert into finding_evidence (finding_id, evidence_id, tenant_id)"
                " values (:f, :e, :t)"
            ),
            {"f": finding_id, "e": evidence_id, "t": ctx.tenant_id},
        )


async def _hypothesis(
    session: AsyncSession,
    ctx: RequestContext,
    incident: str,
    result: AgentResult,
    hypothesis: dict[str, Any],
    support: list[str],
    counter: list[str],
    rank: int,
) -> None:
    hypothesis_id = new_id("hyp")
    await session.execute(
        text("""
            insert into hypotheses (hypothesis_id, tenant_id, incident_id, run_id, agent,
                                    description, category, entities, rank, missing_data)
            values (:id, :t, :incident, :run, :agent, :description, :category,
                    cast(:entities as jsonb), :rank, cast(:missing as jsonb))
        """),
        {
            "id": hypothesis_id,
            "t": ctx.tenant_id,
            "incident": incident,
            "run": result.run_id,
            "agent": result.agent.value,
            "description": hypothesis["description"],
            "category": hypothesis["category"],
            "entities": json.dumps(hypothesis["entities"]),
            "rank": rank,
            "missing": json.dumps(result.missing_data),
        },
    )
    for kind, ids in (("support", support), ("counter", counter)):
        for evidence_id in ids:
            await session.execute(
                text(
                    "insert into hypothesis_evidence (hypothesis_id, evidence_id, tenant_id, kind)"
                    " values (:h, :e, :t, :kind)"
                ),
                {"h": hypothesis_id, "e": evidence_id, "t": ctx.tenant_id, "kind": kind},
            )


def _summary(target: str, leads: list[str], missing: list[str], note: str | None) -> str:
    """A short plain report; the Verifier and a real RCA report come in a later phase."""
    if target == "DIAGNOSED":
        head = f"{len(leads)} evidence-backed hypotheses: " + "; ".join(leads[:3])
    else:
        head = "No hypothesis is backed by evidence."
    parts = [head]
    if missing:
        parts.append("Missing: " + "; ".join(missing[:5]))
    if note:
        parts.append(note)
    return " ".join(parts)[:2000]
