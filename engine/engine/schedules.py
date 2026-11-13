"""Temporal Schedules from each repo's .factory.toml (docs/designs/engine.md, section 4).

One Schedule per (repo, team) with a schedule, ID `schedule/<repo>/<team>`, starting a ScheduleTick: UTC cron,
overlap SKIP (a tick takes a second; the team serialises its runs), catchup window one day (a laptop that slept
through the time runs once on waking), jitter 5 minutes. New schedules start paused until the team has run once by
hand. Reconciling creates, updates (keeping paused and its note) and deletes schedules to match the config."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from engine.factory import config
from engine.models import TeamRef, TickInput
from engine.settings import TASK_QUEUE, EngineSettings

PREFIX = "schedule/"
CATCHUP = timedelta(days=1)
JITTER = timedelta(minutes=5)
NEW_NOTE = "new: run the team once by hand (factory engine run-now), then resume"


@dataclass(frozen=True)
class Desired:
    ref: TeamRef
    cron: str


def desired(settings: EngineSettings) -> dict[str, Desired]:
    """Every scheduled team in the configured repos, as each repo's default branch configures it."""
    out: dict[str, Desired] = {}
    for root in settings.repo_paths():
        local = config.load(root)
        cfg = config.load_ref(root, f"origin/{local.default_branch}")
        for team, spec in cfg.teams().items():
            if spec.schedule and spec.schedule != "off":
                ref = TeamRef(repo=str(root), repo_name=cfg.name, team=team)
                out[ref.schedule_id] = Desired(ref, spec.schedule)
    return out


def build(want: Desired, paused: bool, note: str) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ScheduleTick", TickInput(ref=want.ref), id=f"tick/{want.ref.repo_name}/{want.ref.team}", task_queue=TASK_QUEUE
        ),
        spec=ScheduleSpec(cron_expressions=[want.cron], jitter=JITTER, time_zone_name="UTC"),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP),
        state=ScheduleState(paused=paused, note=note),
    )


async def reconcile(client: Client, settings: EngineSettings) -> list[str]:
    want = desired(settings)
    have = {s.id async for s in await client.list_schedules() if s.id.startswith(PREFIX)}
    report = []
    for sid, w in sorted(want.items()):
        if sid not in have:
            await client.create_schedule(sid, build(w, paused=True, note=NEW_NOTE))
            report.append(f"created {sid} ({w.cron}, paused until a first run by hand)")
            continue
        handle = client.get_schedule_handle(sid)

        def update(inp: ScheduleUpdateInput, w: Desired = w) -> ScheduleUpdate:
            state = inp.description.schedule.state  # keep paused and its note: the team's, not the config's
            return ScheduleUpdate(schedule=build(w, paused=state.paused, note=state.note or ""))

        await handle.update(update)
        report.append(f"updated {sid} ({w.cron})")
    for sid in sorted(have - set(want)):
        await client.get_schedule_handle(sid).delete()
        report.append(f"deleted {sid} (no longer in config)")
    return report
