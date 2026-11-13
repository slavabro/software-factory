"""Temporal Schedules from each repo's .factory.toml (docs/designs/engine.md, section 4).

One Schedule per (repo, team) with a schedule, ID `schedule/<repo>/<team>`, starting a ScheduleTick: cron in the
engine's time zone (engine.toml `time_zone`, default the home machine's; Temporal handles daylight saving),
overlap SKIP (a tick takes a second; the team serialises its runs), catchup window one day (a laptop that slept
through the time runs once on waking), jitter 5 minutes. New schedules start paused until the team has run once by
hand. Reconciling creates, updates (keeping paused and its note) and deletes schedules to match the config. A repo
that can't be read this time (no network, a broken config) keeps its schedules untouched and is reported."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

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
from temporalio.service import RPCError

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


@dataclass
class Wanted:
    schedules: dict[str, Desired] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)  # repo name → why its schedules were left alone


def committed_config(root: Path) -> tuple[str, object | None]:
    """(repo name, its config on origin/<default>, fetched first), or (name, None) when it isn't opted in."""
    local = config.load(root)
    subprocess.run(["git", "fetch", "--quiet", "origin", local.default_branch], cwd=root, capture_output=True, timeout=120)
    ref = f"origin/{local.default_branch}"
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root, capture_output=True).returncode:
        raise ValueError(f"{ref} not found")
    if not config.committed(root, ref):
        return local.name, None
    cfg = config.load_ref(root, ref)
    return cfg.name, cfg


def desired(settings: EngineSettings) -> Wanted:
    """Every scheduled team in the configured repos, as each repo's default branch configures it."""
    out = Wanted()
    for root in settings.repo_paths():
        try:
            name, cfg = committed_config(root)
        except Exception as e:  # noqa: BLE001 — one repo's problem must not touch the others' schedules
            try:
                name = config.load(root).name
            except Exception:  # noqa: BLE001
                name = root.name
            out.unreadable[name] = str(e)[:200]
            continue
        if cfg is None:
            continue  # not opted in
        for team, spec in cfg.teams().items():  # type: ignore[attr-defined]
            if spec.schedule and spec.schedule != "off":
                team_ref = TeamRef(repo=str(root), repo_name=name, team=team)
                out.schedules[team_ref.schedule_id] = Desired(team_ref, spec.schedule)
    return out


def build(want: Desired, paused: bool, note: str, time_zone: str = "UTC") -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ScheduleTick", TickInput(ref=want.ref), id=f"tick/{want.ref.repo_name}/{want.ref.team}", task_queue=TASK_QUEUE
        ),
        spec=ScheduleSpec(cron_expressions=[want.cron], jitter=JITTER, time_zone_name=time_zone),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP),
        state=ScheduleState(paused=paused, note=note),
    )


async def reconcile(client: Client, settings: EngineSettings) -> list[str]:
    want = desired(settings)
    have = {s.id async for s in await client.list_schedules() if s.id.startswith(PREFIX)}
    report = [f"left {name}'s schedules alone: {why}" for name, why in sorted(want.unreadable.items())]
    for sid, w in sorted(want.schedules.items()):
        try:
            if sid not in have:
                await client.create_schedule(sid, build(w, paused=True, note=NEW_NOTE, time_zone=settings.time_zone))
                report.append(f"created {sid} ({w.cron}, paused until a first run by hand)")
                continue

            def update(inp: ScheduleUpdateInput, w: Desired = w) -> ScheduleUpdate:
                state = inp.description.schedule.state  # keep paused and its note: the team's, not the config's
                return ScheduleUpdate(schedule=build(w, paused=state.paused, note=state.note or "", time_zone=settings.time_zone))

            await client.get_schedule_handle(sid).update(update)
            report.append(f"updated {sid} ({w.cron})")
        except RPCError as e:  # e.g. a cron the server refuses: report it, carry on with the rest
            report.append(f"could not set {sid} ({w.cron}): {e.message}")
    kept = tuple(f"{PREFIX}{name}/" for name in want.unreadable)
    for sid in sorted(have - set(want.schedules)):
        if not sid.startswith(kept):
            await client.get_schedule_handle(sid).delete()
            report.append(f"deleted {sid} (no longer in config)")
    return report
