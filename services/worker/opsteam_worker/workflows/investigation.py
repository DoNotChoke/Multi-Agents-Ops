"""IncidentInvestigationWorkflow: plan, run specialists in parallel, conclude, wait for signals.

Deterministic by construction: all I/O, LLM calls and clocks live in activities.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from opsteam_agents.commander import InvestigationPlan
    from opsteam_contracts.agent_result import AgentResult

    from opsteam_worker.workflows.models import (
        WORKFLOW,
        ConcludeInput,
        InvestigationInput,
        Outcome,
        PlanInput,
        SpecialistInput,
        Started,
        Status,
    )

# Infrastructure hiccups get two more tries; refusals and bad data never get better by retrying.
RETRY = RetryPolicy(
    maximum_attempts=3,
    non_retryable_error_types=[
        "BudgetExceeded", "ValidationError", "IncidentNotFound", "InvalidTransition",
    ],
)  # fmt: skip
SHORT = timedelta(seconds=30)
# A specialist's own deadline is 120 s; the margin covers its last LLM and tool call.
SPECIALIST = timedelta(seconds=180)
WAIT = timedelta(hours=24)
MAX_ROUNDS = 3


@workflow.defn(name=WORKFLOW)
class IncidentInvestigationWorkflow:
    def __init__(self) -> None:
        self.status = Status()
        self.questions: list[str] = []
        self.stop: str | None = None

    @workflow.run
    async def run(self, start: InvestigationInput) -> Status:
        question, inconclusive = start.question, False
        while True:
            self.status.round += 1
            current = start.model_copy(update={"question": question})
            outcome = await self._round(current, inconclusive)
            inconclusive = outcome.status == "INCONCLUSIVE"
            self.status.phase = "waiting"
            try:
                await workflow.wait_condition(
                    lambda: bool(self.questions) or self.stop is not None, timeout=WAIT
                )
            except TimeoutError:
                break
            if self.stop is not None or self.status.round >= MAX_ROUNDS:
                break
            question = self.questions.pop(0)
        self.status.phase = self.stop or "closed"
        return self.status

    @workflow.signal
    def follow_up(self, question: str) -> None:
        self.questions.append(question[:2000])

    @workflow.signal
    def cancel(self) -> None:
        self.stop = "cancelled"

    @workflow.signal
    def resolved(self) -> None:
        self.stop = "resolved"

    @workflow.query
    def get_status(self) -> Status:
        return self.status

    async def _round(self, start: InvestigationInput, inconclusive: bool) -> Outcome:
        self.status.phase = "starting"
        started = await workflow.execute_activity(
            "begin_investigation",
            start,
            result_type=Started,
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        if started.brief is None:
            return await self._conclude(start, [], "no service is known for this incident")
        self.status.phase = "planning"
        plan = await workflow.execute_activity(
            "plan_investigation",
            PlanInput(input=start, brief=started.brief, prior_inconclusive=inconclusive),
            result_type=InvestigationPlan,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=RETRY,
        )
        self.status.plan = plan
        self.status.phase = "investigating"
        self.status.agents = {t.agent: "running" for t in plan.tasks}
        runs = [
            workflow.execute_activity(
                "run_specialist",
                SpecialistInput(
                    input=start,
                    brief=started.brief,
                    task=task,
                    tier=plan.tier,
                    integrations=started.integrations,
                ),
                result_type=AgentResult,
                start_to_close_timeout=SPECIALIST,
                retry_policy=RETRY,
            )
            for task in plan.tasks
        ]
        results: list[AgentResult] = []
        outcomes = await asyncio.gather(*runs, return_exceptions=True)
        for task, outcome in zip(plan.tasks, outcomes, strict=True):
            if isinstance(outcome, AgentResult):
                results.append(outcome)
                self.status.agents[task.agent] = outcome.status.value
            else:  # one failed specialist does not sink the others' results
                self.status.agents[task.agent] = "failed"
        failed = [a for a, s in self.status.agents.items() if s == "failed"]
        return await self._conclude(
            start, results, f"failed: {', '.join(failed)}" if failed else None
        )

    async def _conclude(
        self, start: InvestigationInput, results: list[AgentResult], note: str | None
    ) -> Outcome:
        self.status.phase = "concluding"
        outcome = await workflow.execute_activity(
            "conclude_investigation",
            ConcludeInput(input=start, results=results, note=note),
            result_type=Outcome,
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        self.status.outcome = outcome
        return Outcome.model_validate(outcome)
