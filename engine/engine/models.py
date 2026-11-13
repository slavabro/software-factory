"""Every input and output that crosses a workflow, activity or message boundary: one Pydantic model per call.
Pure (no I/O), so workflows import it through the sandbox as a passthrough module."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

Reason = Literal["schedule", "ci_red", "deploy_failed", "incident", "merged", "decision", "manual"]
Outcome = Literal["done", "failed", "interrupted", "skipped"]
SEEN_LIMIT = 200  # event ids a team remembers for dedupe


class Wake(BaseModel):
    reason: Reason
    event_id: str
    details: dict[str, str] = Field(default_factory=dict)


class TeamRef(BaseModel):
    repo: str  # absolute path of the repo's main checkout
    repo_name: str
    team: str

    @property
    def workflow_id(self) -> str:
        return f"team/{self.repo_name}/{self.team}"

    @property
    def schedule_id(self) -> str:
        return f"schedule/{self.repo_name}/{self.team}"


class TeamInput(BaseModel):
    ref: TeamRef
    pending: list[Wake] = Field(default_factory=list)  # carried across continue-as-new
    seen: list[str] = Field(default_factory=list)
    runs: int = 0


class RunPaths(BaseModel):
    worktree: str
    branch: str
    log: str
    report: str


class ResolveInput(BaseModel):
    ref: TeamRef
    run_id: str
    day: str  # YYYYMMDD, from workflow time


class TeamPlan(BaseModel):
    enabled: bool
    reason: str = ""  # why not enabled
    prompt: str = ""
    budget_minutes: int = 90
    paths: RunPaths | None = None


class BudgetAnswer(BaseModel):
    verdict: Literal["go", "wait", "stop"]
    until: AwareDatetime | None = None  # with "wait"; timezone-aware, so workflow time arithmetic is safe
    reason: str = ""


class StepInput(BaseModel):
    ref: TeamRef
    paths: RunPaths
    prompt: str = ""


class StepResult(BaseModel):
    ok: bool
    message: str = ""


class OutcomeInput(BaseModel):
    ref: TeamRef
    run_id: str
    outcome: Outcome
    summary: str
    reasons: list[Reason]
    timed_out: bool = False  # a step timed out: interrupted if the machine slept during the run, else failed
    log: str | None = None  # the run's log; a sleep leaves a marker next to it


class OutcomeResult(BaseModel):
    outcome: Outcome  # as recorded (a timeout across a sleep becomes interrupted)
    consecutive_failures: int
    paused: bool


class TeamResult(BaseModel):
    outcome: Outcome
    summary: str


class WakeTeamInput(BaseModel):
    ref: TeamRef
    wake: Wake
    bypass_pause: bool = False  # `factory engine run-now`


class WakeTeamResult(BaseModel):
    woken: bool
    reason: str = ""


class TickInput(BaseModel):
    ref: TeamRef
