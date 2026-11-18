"""`factory engine …` commands that talk to the engine's Temporal (the standard-library `factory` CLI runs this
module with the engine's own Python). Argument parsing and plain output, one line per fact; the operations
themselves are engine/ops.py, which the desk shares."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Awaitable, Callable

from temporalio.client import Client

from factory_runtime.temporal import get_client
from engine import ops, settings as settings_mod
from engine.factory import config
from engine.settings import DEPLOYMENT, EngineSettings
from engine.worker import build_id

Command = Callable[[Client, EngineSettings, argparse.Namespace], Awaitable[int]]


def _minutes(when: datetime | None) -> str:
    return when.isoformat(timespec="minutes") if when is not None else ""


async def cmd_signal(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    try:
        details = json.loads(a.details) if a.details else {}
        if not isinstance(details, dict) or not all(isinstance(v, str) for v in details.values()):
            raise ValueError
    except ValueError:
        raise SystemExit('--details must be a JSON object of strings, e.g. \'{"run": "123"}\'') from None
    woken = await ops.signal(client, settings, a.repo, a.reason, a.event_id, details, a.team)
    if not woken:
        print(f"no team in {a.repo} wakes on {a.reason}")
    for s in woken:
        print(f"{s.ref.workflow_id}: {'woken' if s.result.woken else 'not woken — ' + s.result.reason}")
    return 0


async def cmd_run_now(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    res = await ops.run_now(client, settings, ops.team_ref(a.repo, a.team))
    print(f"{res.ref.workflow_id}: woken by hand (runs even while its schedule is paused)")
    if not res.unattended:
        print("the spend switch is off (factory engine spend on): the run stops before it starts an agent session")
    return 0


async def cmd_pause(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    ref = ops.team_ref(a.repo, a.team)
    act = ops.pause if a.command == "pause" else ops.resume
    change = await act(client, settings, ref, a.note)
    print(f"{change.target}: {a.command}d")
    return 0


async def cmd_reconcile(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    for line in await ops.reconcile(client, settings):
        print(line)
    return 0


def run_line(run: ops.RunView) -> str:
    if run.status == "running":
        return f"{run.workflow_id}: running since {_minutes(run.started)}"
    line = f"{run.workflow_id}: {run.status} {_minutes(run.closed)}"
    if run.status == "continued_as_new":
        return line + " — continued with the wakes that arrived during the run"
    if run.outcome is not None:
        return line + f" — {run.outcome}: {run.summary[:120]}"
    return line + f" — {(run.failure or 'no reason recorded')[:160]}"


async def cmd_status(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    snap = await ops.snapshot(client, settings, recent=a.recent)
    for s in snap.schedules:
        state = f"PAUSED ({s.note or 'no note'})" if s.paused else "active"
        print(f"{s.schedule_id}: {state}, next {_minutes(s.next_run) or '—'}")
    for run in (*snap.open_runs, *snap.recent_runs):
        print(run_line(run))
    if not snap.settings.unattended:
        print("spend switch: off — no team starts an agent session (factory engine spend on)")
    return 0


async def cmd_set_current(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    build = a.build_id or build_id()
    await ops.set_current(client, settings, build)
    print(f"{DEPLOYMENT}: current build {build}")
    return 0


async def cmd_drained(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    """Exit 0 when the build's worker may stop: drained, unknown to the server, or never current and nothing pinned
    to it. Exit 1 while it is current or runs remain pinned to it."""
    s = await ops.build_status(client, a.build_id)
    if not s.known:
        print(f"{DEPLOYMENT} build {a.build_id}: unknown to the server (never ran)")
    else:
        print(f"{DEPLOYMENT} build {a.build_id}: {'current' if s.current else s.drainage}"
              f"{' (never current)' if s.never_current else ''}")
    return 0 if s.may_stop else 1


async def cmd_repo(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    if a.action == "add":
        res = await ops.add_repo(client, settings, a.path)
        print(f"{res.entry}: {"already one of the engine's repos" if res.unchanged else 'added'} ({res.name})")
    else:
        res = await ops.remove_repo(client, settings, a.path)
        print(f"{res.entry}: removed ({res.name})")
    for line in res.report:
        print(line)
    for run in res.open_runs:
        print(f"{run.workflow_id}: still running; it finishes, nothing new starts")
    return 0


def cmd_spend(settings: EngineSettings, a: argparse.Namespace) -> int:
    on = a.state == "on"
    change = ops.set_unattended(settings, on)
    was = "" if change.before != change.after else " (it already was)"
    if on:
        print(f"spend switch on{was}: teams may start agent sessions")
    else:
        print(f"spend switch off{was}: no team starts a new agent session; a session already running finishes")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory engine", description="The factory engine's commands")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("signal", help="wake the teams that listen for an event")
    s.add_argument("reason", choices=[*config.WAKE_REASONS, "decision"])
    s.add_argument("--repo", required=True)
    s.add_argument("--team")
    s.add_argument("--event-id", required=True)
    s.add_argument("--details", help="JSON object of strings")
    r = sub.add_parser("run-now", help="run a team now, even while its schedule is paused")
    r.add_argument("--repo", required=True)
    r.add_argument("--team", required=True)
    for name in ("pause", "resume"):
        q = sub.add_parser(name, help=f"{name} a team's schedule")
        q.add_argument("--repo", required=True)
        q.add_argument("--team", required=True)
        q.add_argument("--note")
    sub.add_parser("reconcile", help="make the schedules match every repo's .factory.toml")
    st = sub.add_parser("status", help="schedules, running teams, recent runs")
    st.add_argument("--recent", type=int, default=10)
    sp = sub.add_parser("spend", help="the spend switch: off stops new agent sessions, a running one finishes")
    sp.add_argument("state", choices=["on", "off"])
    rp = sub.add_parser("repo", help="add or remove a repo the engine runs teams for")
    rp.add_argument("action", choices=["add", "remove"])
    rp.add_argument("path")
    sub.add_parser("build-id", help="this checkout's Build ID")
    c = sub.add_parser("set-current", help="route new runs to a Build ID")
    c.add_argument("--build-id")
    d = sub.add_parser("drained", help="exit 0 once nothing open is pinned to a Build ID")
    d.add_argument("--build-id", required=True)
    return p


COMMANDS: dict[str, Command] = {
    "signal": cmd_signal, "run-now": cmd_run_now, "pause": cmd_pause, "resume": cmd_pause, "reconcile": cmd_reconcile,
    "status": cmd_status, "set-current": cmd_set_current, "drained": cmd_drained, "repo": cmd_repo,
}


async def run(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if a.command == "build-id":
        print(build_id())
        return 0
    settings = settings_mod.load()
    try:
        if a.command == "spend":  # a file change: works while the server is down
            return cmd_spend(settings, a)
        client = await get_client(settings.temporal)
        return await COMMANDS[a.command](client, settings, a)
    except ops.OpError as e:
        raise SystemExit(f"factory engine: {e}") from None


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
