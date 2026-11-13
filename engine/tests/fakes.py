"""Stand-in activities registered under the real names, so workflow tests exercise the real workflows."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engine.models import (
    BudgetAnswer,
    OutcomeInput,
    OutcomeResult,
    ResolveInput,
    RunPaths,
    StepInput,
    StepResult,
    TeamPlan,
    TeamRef,
    WakeTeamInput,
    WakeTeamResult,
)


class Fakes:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.outcomes: list[OutcomeInput] = []
        self.woken: list[WakeTeamInput] = []
        self.enabled = True
        self.budget: list[BudgetAnswer] = []  # answers in order; then "go"
        self.agent_error: str | None = None  # ApplicationError type to raise
        self.hold = asyncio.Event()  # the agent waits for this when `holding`
        self.holding = False
        self.agent_started = asyncio.Event()
        self.agent_cancelled = False
        self.resolve_errors = 0  # resolve_team raises (non-retryable) this many times
        self.resolve_holding = False
        self.resolve_started = asyncio.Event()
        self.prepare_holding = False
        self.prepare_started = asyncio.Event()
        self.prepare_cancelled = False

    def activities(self) -> list:
        f = self

        @activity.defn(name="resolve_team")
        async def resolve_team(inp: ResolveInput) -> TeamPlan:
            f.calls.append("resolve_team")
            f.resolve_started.set()
            while f.resolve_holding:
                activity.heartbeat()
                await asyncio.sleep(0.05)
            if f.resolve_errors:
                f.resolve_errors -= 1
                raise ApplicationError("config unreadable", type="ConfigInvalid", non_retryable=True)
            if not f.enabled:
                return TeamPlan(enabled=False, reason="no such team")
            paths = RunPaths(worktree=f"/w/{inp.run_id[:8]}", branch=f"factory/{inp.ref.team}/{inp.day}-{inp.run_id[:8]}",
                             log="/l.log", report="/l.report.md")
            return TeamPlan(enabled=True, prompt="/factory:qa", budget_minutes=5, paths=paths)

        @activity.defn(name="check_budget")
        async def check_budget(ref: TeamRef) -> BudgetAnswer:
            f.calls.append("check_budget")
            return f.budget.pop(0) if f.budget else BudgetAnswer(verdict="go")

        @activity.defn(name="prepare_worktree")
        async def prepare_worktree(inp: StepInput) -> StepResult:
            f.calls.append("prepare_worktree")
            f.prepare_started.set()
            try:
                while f.prepare_holding:
                    activity.heartbeat()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                f.prepare_cancelled = True
                raise
            return StepResult(ok=True)

        @activity.defn(name="run_agent_session")
        async def run_agent_session(inp: StepInput) -> StepResult:
            f.calls.append("run_agent_session")
            f.agent_started.set()
            if f.agent_error:
                raise ApplicationError("agent said no", type=f.agent_error, non_retryable=True)
            try:
                while f.holding and not f.hold.is_set():
                    activity.heartbeat()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                f.agent_cancelled = True
                raise
            return StepResult(ok=True, message="worked")

        @activity.defn(name="deliver_report")
        async def deliver_report(inp: StepInput) -> StepResult:
            f.calls.append("deliver_report")
            return StepResult(ok=True, message="report sent")

        @activity.defn(name="remove_worktree")
        async def remove_worktree(inp: StepInput) -> StepResult:
            f.calls.append("remove_worktree")
            return StepResult(ok=True, message="cleaned")

        @activity.defn(name="record_outcome")
        async def record_outcome(inp: OutcomeInput) -> OutcomeResult:
            f.calls.append("record_outcome")
            f.outcomes.append(inp)
            return OutcomeResult(outcome=inp.outcome, consecutive_failures=0, paused=False)

        @activity.defn(name="wake_team")
        async def wake_team(inp: WakeTeamInput) -> WakeTeamResult:
            f.woken.append(inp)
            return WakeTeamResult(woken=True)

        return [resolve_team, check_budget, prepare_worktree, run_agent_session, deliver_report, remove_worktree,
                record_outcome, wake_team]


def later(hours: float) -> BudgetAnswer:
    return BudgetAnswer(verdict="wait", until=datetime.now(timezone.utc) + timedelta(hours=hours))
