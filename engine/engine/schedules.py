"""Temporal Schedules from each repo's .factory.toml (docs/designs/engine.md, section 4).

One Schedule per (repo, team) with a schedule, ID `schedule/<repo>/<team>`, starting a ScheduleTick: cron in the
engine's time zone (engine.toml `time_zone`, default the home machine's; Temporal handles daylight saving),
overlap SKIP (a tick takes a second; the team serialises its runs), catchup window one day (a laptop that slept
through the time runs once on waking), jitter 5 minutes. New schedules start paused until the team has run once by
hand. Reconciling creates, updates (keeping paused and its note) and deletes schedules to match the config. A repo
that can't be read this time (no network, a broken config) keeps its schedules untouched and is reported.

An unchanged schedule is left alone and not reported. The server keeps a cron as calendars, so the cron as described
can't be compared with the one built here. Instead each schedule carries a digest of what reconcile built
(`spec_digest`, in its action's memo, which an update can change and a describe returns as given; being the action's
memo, it is also on each tick run's memo), and a schedule is updated when its digest differs, or when a field whose
server shape is stable has drifted (`drifted`: time zone, jitter, overlap, catchup window, the action's workflow, ID,
task queue and input), e.g. after an edit in Temporal's UI. A cron edited there is put back only when the config or
`build` next changes, since the cron itself can't be compared. A change to `build` changes every digest, so each
schedule is updated once."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from dataclasses import dataclass, field, replace
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
from temporalio.api.common.v1 import Payload
from temporalio.service import RPCError

from engine.factory import config
from engine.models import TeamRef, TickInput
from engine.settings import TASK_QUEUE, EngineSettings

PREFIX = "schedule/"
CATCHUP = timedelta(days=1)
JITTER = timedelta(minutes=5)
NEW_NOTE = "new: run the team once by hand (factory engine run-now), then resume"
DIGEST = "factory_schedule"  # the action memo key holding `spec_digest`


@dataclass(frozen=True)
class Desired:
    ref: TeamRef
    cron: str


@dataclass
class Wanted:
    schedules: dict[str, Desired] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)  # repo name → why its schedules were left alone


def fetch_default(root: Path) -> None:
    """Bring origin/<default> up to date (best effort: offline, the last fetched ref stands). Only actions fetch."""
    branch = config.load(root).default_branch
    subprocess.run(["git", "fetch", "--quiet", "origin", branch], cwd=root, capture_output=True, timeout=120)


def committed_config(root: Path) -> tuple[str, object | None]:
    """(repo name, its config on the already-fetched origin/<default>), or (name, None) when it isn't opted in.
    Never fetches, so a read (status, the desk) stays local and quick."""
    local = config.load(root)
    ref = f"origin/{local.default_branch}"
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root, capture_output=True).returncode:
        raise ValueError(f"{ref} not found")
    if not config.committed(root, ref):
        return local.name, None
    cfg = config.load_ref(root, ref)
    return cfg.name, cfg


def desired(settings: EngineSettings) -> Wanted:
    """Every scheduled team in the configured repos, as each repo's default branch configures it (fetched first:
    reconciling is an action)."""
    out = Wanted()
    for root in settings.repo_paths():
        try:
            fetch_default(root)
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


def spec_digest(schedule: Schedule) -> str:
    """What reconcile set, as a digest: everything in the schedule but its state (paused and the note are the
    team's) and the digest itself."""
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    bare = replace(schedule, state=ScheduleState(), action=replace(action, memo=None))
    return hashlib.sha256(repr(bare).encode()).hexdigest()[:16]


def build(want: Desired, paused: bool, note: str, time_zone: str = "UTC") -> Schedule:
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "ScheduleTick", TickInput(ref=want.ref), id=f"tick/{want.ref.repo_name}/{want.ref.team}", task_queue=TASK_QUEUE
        ),
        spec=ScheduleSpec(cron_expressions=[want.cron], jitter=JITTER, time_zone_name=time_zone),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=CATCHUP),
        state=ScheduleState(paused=paused, note=note),
    )
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    schedule.action.memo = {DIGEST: spec_digest(schedule)}
    return schedule


def drifted(client: Client, have: Schedule, want: Schedule) -> bool:
    """Whether the schedule on the server differs from the built one in a field the server returns as given."""
    a, b = have.action, want.action
    if not (isinstance(a, ScheduleActionStartWorkflow) and isinstance(b, ScheduleActionStartWorkflow)):
        return True
    try:
        wanted = client.data_converter.payload_converter.to_payloads(list(b.args))
    except Exception:  # noqa: BLE001 — can't tell: set it again
        return True
    got = list(a.args)
    same_args = len(got) == len(wanted) and all(
        isinstance(g, Payload) and g.data == w.data and dict(g.metadata) == dict(w.metadata) for g, w in zip(got, wanted)
    )

    def fields(s: Schedule, x: ScheduleActionStartWorkflow) -> tuple[object, ...]:
        return (s.spec.time_zone_name, s.spec.jitter, s.policy.overlap, s.policy.catchup_window, x.workflow, x.id,
                x.task_queue)

    return not same_args or fields(have, a) != fields(want, b)


def described_digest(client: Client, schedule: Schedule) -> str | None:
    """The digest a schedule on the server carries; None for one made before schedules carried it."""
    action = schedule.action
    raw = action.memo.get(DIGEST) if isinstance(action, ScheduleActionStartWorkflow) and action.memo else None
    if raw is None:
        return None
    try:
        value = client.data_converter.payload_converter.from_payload(raw, str)
    except Exception:  # noqa: BLE001 — a memo someone else wrote: not ours, so update it
        return None
    return value if isinstance(value, str) else None


async def reconcile(client: Client, settings: EngineSettings) -> list[str]:
    want = await asyncio.to_thread(desired, settings)  # git fetches: off the event loop (the desk shares it)
    have = {s.id async for s in await client.list_schedules() if s.id.startswith(PREFIX)}
    report = [f"left {name}'s schedules alone: {why}" for name, why in sorted(want.unreadable.items())]
    for sid, w in sorted(want.schedules.items()):
        try:
            if sid not in have:
                await client.create_schedule(sid, build(w, paused=True, note=NEW_NOTE, time_zone=settings.time_zone))
                report.append(f"created {sid} ({w.cron}, paused until a first run by hand)")
                continue

            changed: list[bool] = []

            def update(inp: ScheduleUpdateInput, w: Desired = w, changed: list[bool] = changed) -> ScheduleUpdate | None:
                state = inp.description.schedule.state  # keep paused and its note: the team's, not the config's
                new = build(w, paused=state.paused, note=state.note or "", time_zone=settings.time_zone)
                have = inp.description.schedule
                if described_digest(client, have) == spec_digest(new) and not drifted(client, have, new):
                    return None  # unchanged: left alone
                changed.append(True)
                return ScheduleUpdate(schedule=new)

            await client.get_schedule_handle(sid).update(update)
            if changed:
                report.append(f"updated {sid} ({w.cron})")
        except RPCError as e:  # e.g. a cron the server refuses: report it, carry on with the rest
            report.append(f"could not set {sid} ({w.cron}): {e.message}")
    kept = tuple(f"{PREFIX}{name}/" for name in want.unreadable)
    for sid in sorted(have - set(want.schedules)):
        if not sid.startswith(kept):
            await client.get_schedule_handle(sid).delete()
            report.append(f"deleted {sid} (no longer in config)")
    return report
