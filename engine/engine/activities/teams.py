"""Activities that need the engine's Temporal client or its state: the spend check, recording an outcome (and
pausing a team after repeated failures), and waking a team (docs/designs/engine.md, sections 3–5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.temporal import activity_idempotency_key, signal_with_start
from engine.activities.agent import slept_marker
from engine.models import (
    BudgetAnswer,
    OutcomeInput,
    OutcomeResult,
    TeamInput,
    TeamRef,
    WakeTeamInput,
    WakeTeamResult,
)
from engine import settings as settings_mod
from engine.settings import TASK_QUEUE, EngineSettings

PAUSE_AFTER_FAILURES = 3
REPO_KEY = SearchAttributeKey.for_keyword("FactoryRepo")
TEAM_KEY = SearchAttributeKey.for_keyword("FactoryTeam")


def team_search_attributes(ref: TeamRef) -> TypedSearchAttributes:
    return TypedSearchAttributes([SearchAttributePair(REPO_KEY, ref.repo_name), SearchAttributePair(TEAM_KEY, ref.team)])


async def schedule_paused(client: Client, ref: TeamRef) -> tuple[bool, str]:
    """(paused, note) of the team's Schedule, the one owner of "paused"; a team with no schedule is not paused."""
    try:
        desc = await client.get_schedule_handle(ref.schedule_id).describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return False, ""
        raise
    return desc.schedule.state.paused, desc.schedule.state.note or ""


async def wake(client: Client, settings: EngineSettings, inp: WakeTeamInput) -> WakeTeamResult:
    """Signal-with-start the team; refused while its schedule is paused unless `bypass_pause`."""
    if not inp.bypass_pause:
        paused, note = await schedule_paused(client, inp.ref)
        if paused:
            return WakeTeamResult(woken=False, reason=f"paused: {note or 'no note'}")
    options: dict[str, Any] = {"id_reuse_policy": WorkflowIDReusePolicy.ALLOW_DUPLICATE}
    if settings.search_attributes:
        options["search_attributes"] = team_search_attributes(inp.ref)
    await signal_with_start(
        client,
        "TeamWorkflow",
        inp.ref.workflow_id,
        task_queue=TASK_QUEUE,
        signal="wake",
        signal_args=[inp.wake],
        args=[TeamInput(ref=inp.ref)],
        **options,
    )
    return WakeTeamResult(woken=True)


class TeamActivities:
    def __init__(self, client: Client, settings: EngineSettings) -> None:
        self.client = client
        self.settings = settings

    def _team_dir(self, ref: TeamRef) -> Path:
        return self.settings.state / "teams" / ref.repo_name / ref.team

    @activity.defn(name="check_budget")
    async def check_budget(self, ref: TeamRef) -> BudgetAnswer:
        """The spend switch, read fresh on every run so turning it off takes effect at once; plan usage windows
        (wait until the window reopens) join with F-41."""
        if not settings_mod.load().unattended:
            return BudgetAnswer(verdict="stop", reason="unattended runs are off (engine.toml: unattended = false)")
        return BudgetAnswer(verdict="go")

    @activity.defn(name="record_outcome")
    async def record_outcome(self, inp: OutcomeInput) -> OutcomeResult:
        """Record the run once however often retried: the team's state (failures in a row) first, written atomically,
        then a notebook entry. A timeout during which the machine slept is `interrupted`; interrupted and skipped
        runs don't count. At PAUSE_AFTER_FAILURES the team's schedule is paused, on every attempt (pausing twice is
        harmless), so a pause that failed once is not lost."""
        key = activity_idempotency_key()
        team_dir = self._team_dir(inp.ref)
        outcome = inp.outcome
        if inp.timed_out and inp.log and slept_marker(inp.log).is_file():
            outcome = "interrupted"

        def write() -> tuple[int, bool]:
            team_dir.mkdir(parents=True, exist_ok=True)
            state_path = team_dir / "state.json"
            raw = json.loads(state_path.read_text()) if state_path.is_file() else {}
            failures: int = int(raw.get("failures", 0))
            recorded: list[str] = list(raw.get("recorded", []))
            if key in recorded:
                return failures, False
            if outcome == "failed":
                failures += 1
            elif outcome == "done":
                failures = 0
            recorded = [*recorded, key][-50:]
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"failures": failures, "recorded": recorded}))
            tmp.replace(state_path)
            with open(team_dir / "notebook.md", "a") as notebook:
                notebook.write(
                    f"\n## {inp.run_id[:8]} · {outcome} · woken by {', '.join(inp.reasons) or 'nothing'}\n\n"
                    f"{inp.summary or '(no summary)'}\n"
                )
            return failures, True

        failures, _ = await asyncio.to_thread(write)
        paused = False
        if failures >= PAUSE_AFTER_FAILURES:
            try:
                await self.client.get_schedule_handle(inp.ref.schedule_id).pause(
                    note=f"paused after {failures} failed runs in a row; last: {inp.summary[:200]}"
                )
                paused = True
            except RPCError as e:
                if e.status != RPCStatusCode.NOT_FOUND:
                    raise
        return OutcomeResult(outcome=outcome, consecutive_failures=failures, paused=paused)

    @activity.defn(name="wake_team")
    async def wake_team(self, inp: WakeTeamInput) -> WakeTeamResult:
        return await wake(self.client, self.settings, inp)
