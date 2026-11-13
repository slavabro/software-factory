"""TeamWorkflow: one per (repo, team), alive only while there is work (docs/designs/engine.md, section 3).

Woken by the `wake` signal through signal-with-start. Runs one improvement run per batch of wakes; wakes that arrive
during a run are merged and handled by a continue-as-new; with none left it completes, so an idle team holds nothing
open and a new worker version takes every new run (Pinned)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, TimeoutError

with workflow.unsafe.imports_passed_through():
    from engine.models import (
        SEEN_LIMIT,
        BudgetAnswer,
        OutcomeInput,
        Reason,
        OutcomeResult,
        Outcome,
        ResolveInput,
        StepInput,
        StepResult,
        TeamInput,
        TeamPlan,
        TeamResult,
        Wake,
    )

SHORT = RetryPolicy(maximum_attempts=3)
PATIENT = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=5), backoff_coefficient=2)
HEARTBEAT_TIMEOUT = timedelta(minutes=3)
FIRST_WAKE_TIMEOUT = timedelta(minutes=1)  # started without a wake (never by the engine itself): finish
MAX_BUDGET_WAITS = 3


def classify(error: ActivityError) -> tuple[Outcome, str, bool]:
    """(outcome, summary, timed out). A timeout is `failed` here; record_outcome turns it into `interrupted` when the
    machine slept during the run (a hung or over-budget agent stays a failure and counts toward pausing the team).
    A cancelled step is `skipped`."""
    cause = error.cause
    if isinstance(cause, TimeoutError):
        return "failed", f"timed out ({cause.type.name.lower() if cause.type else 'unknown'})", True
    if isinstance(cause, CancelledError):
        return "skipped", "cancelled", False
    if isinstance(cause, ApplicationError) and cause.type == "AgentInterrupted":
        return "interrupted", cause.message[:500], False
    return "failed", (cause.message if isinstance(cause, ApplicationError) else str(cause or error))[:500], False


@workflow.defn(name="TeamWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class TeamWorkflow:
    @workflow.init
    def __init__(self, inp: TeamInput) -> None:
        self.pending: list[Wake] = list(inp.pending)
        self.budget_minutes = 0
        self.seen: list[str] = list(inp.seen)
        self.state = "waiting"

    @workflow.signal
    def wake(self, wake: Wake) -> None:
        if wake.event_id in self.seen:
            return  # signals arrive at least once
        self.seen = [*self.seen, wake.event_id][-SEEN_LIMIT:]
        self.pending.append(wake)

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.run
    async def run(self, inp: TeamInput) -> TeamResult:
        try:
            await workflow.wait_condition(lambda: bool(self.pending), timeout=FIRST_WAKE_TIMEOUT)
        except asyncio.TimeoutError:
            return TeamResult(outcome="skipped", summary="started without a wake")
        wakes, self.pending = self.pending, []
        try:
            result = await self._run_once(inp, wakes)
        except ActivityError as e:
            if isinstance(e.cause, CancelledError):  # cancelled while resolving or checking the budget
                await self._record(inp, "skipped", "cancelled", sorted({w.reason for w in wakes}))
                raise asyncio.CancelledError() from None
            # A step outside the agent's (resolve, budget, clean, record) gave up. Record it if possible, and still
            # carry pending wakes on below rather than failing the workflow and losing them.
            outcome, summary, timed_out = classify(e)
            try:
                result = await self._record(inp, outcome, f"run stopped: {summary}", sorted({w.reason for w in wakes}),
                                            timed_out=timed_out)
            except ActivityError:
                result = TeamResult(outcome=outcome, summary=f"run stopped (not recorded): {summary}")
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.pending:  # wakes that arrived during the run: one more run, on fresh history
            workflow.continue_as_new(
                TeamInput(ref=inp.ref, pending=self.pending, seen=self.seen, runs=inp.runs + 1)
            )
        return result

    async def _run_once(self, inp: TeamInput, wakes: list[Wake]) -> TeamResult:
        ref = inp.ref
        info = workflow.info()
        reasons = sorted({w.reason for w in wakes})
        self.state = "resolving"
        plan = await workflow.execute_activity(
            "resolve_team",
            ResolveInput(ref=ref, run_id=info.run_id, day=workflow.now().strftime("%Y%m%d")),
            result_type=TeamPlan,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=SHORT,
            summary="read the team's config",
        )
        if not plan.enabled or plan.paths is None:
            return await self._record(inp, "skipped", plan.reason or "team not enabled", reasons)

        for _ in range(MAX_BUDGET_WAITS):
            self.state = "budget"
            budget = await workflow.execute_activity(
                "check_budget",
                ref,
                result_type=BudgetAnswer,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=SHORT,
                summary="check AI usage limits and the spend switch",
            )
            if budget.verdict != "wait" or budget.until is None:
                break
            self.state = f"waiting for budget until {budget.until.isoformat()}"
            await workflow.sleep(max(budget.until - workflow.now(), timedelta(seconds=1)))
        if budget.verdict != "go":
            return await self._record(inp, "skipped", budget.reason or "no budget", reasons)

        step = StepInput(ref=ref, paths=plan.paths, prompt=plan.prompt)
        outcome: Outcome = "failed"
        summary = ""
        cancelled = timed_out = False
        log = plan.paths.log
        self.budget_minutes = plan.budget_minutes
        try:
            self.state = "preparing"
            prepared = await workflow.execute_activity(
                "prepare_worktree",
                step,
                result_type=StepResult,
                start_to_close_timeout=timedelta(minutes=35),  # the repo's own setup may take a while
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=SHORT,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                summary="fresh worktree and the repo's setup",
            )
            if not prepared.ok:
                outcome = "skipped" if prepared.message.startswith("not opted in") else "failed"
                summary = prepared.message
                return await self._record(inp, outcome, summary, reasons, log=log)
            self.state = "working"
            budget_time = timedelta(minutes=plan.budget_minutes)
            session = await workflow.execute_activity(
                "run_agent_session",
                step,
                result_type=StepResult,
                start_to_close_timeout=budget_time,
                schedule_to_close_timeout=budget_time + timedelta(minutes=30),
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=RetryPolicy(
                    maximum_attempts=2, non_retryable_error_types=["AgentFailed", "AgentMissing"]
                ),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                summary=f"agent session: {plan.prompt}",
            )
            self.state = "delivering"
            delivered = await workflow.execute_activity(
                "deliver_report",
                step,
                result_type=StepResult,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=PATIENT,
                summary="collect and send the run's report",
            )
            outcome, summary = "done", delivered.message or session.message
        except ActivityError as e:
            outcome, summary, timed_out = classify(e)
            cancelled = isinstance(e.cause, CancelledError)
        finally:
            self.state = "cleaning"
            # After a cancel too: the session activity has finished (WAIT_CANCELLATION_COMPLETED), so no agent is
            # still writing in the worktree.
            await workflow.execute_activity(
                "remove_worktree",
                step,
                result_type=StepResult,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=SHORT,
                summary="remove the run's worktree (keeps unpushed work)",
            )
        result = await self._record(inp, outcome, summary, reasons, timed_out=timed_out, log=log)
        if cancelled:  # cleaned up and recorded; now end as cancelled, as asked
            raise asyncio.CancelledError()
        return result

    async def _record(
        self,
        inp: TeamInput,
        outcome: Outcome,
        summary: str,
        reasons: list[Reason],
        *,
        timed_out: bool = False,
        log: str | None = None,
    ) -> TeamResult:
        self.state = "recording"
        recorded: OutcomeResult = await workflow.execute_activity(
            "record_outcome",
            OutcomeInput(ref=inp.ref, run_id=workflow.info().run_id, outcome=outcome, summary=summary, reasons=reasons,
                         timed_out=timed_out, log=log, budget_minutes=self.budget_minutes),
            result_type=OutcomeResult,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=PATIENT,
            summary=f"record {outcome}",
        )
        self.state = recorded.outcome
        return TeamResult(outcome=recorded.outcome, summary=summary)
