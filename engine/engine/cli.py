"""`factory engine …` commands that talk to the engine's Temporal (the standard-library `factory` CLI runs this
module with the engine's own Python). Plain output, one line per fact."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import VersionDrainageStatus, WorkerDeploymentVersionStatus
from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkerDeploymentVersion
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.temporal import get_client, get_run_failure
from engine import schedules, settings as settings_mod
from engine.activities.teams import schedule_paused, wake
from engine.factory import config
from engine.models import TeamRef, Wake, WakeTeamInput
from engine.settings import DEPLOYMENT, NAMESPACE, EngineSettings
from engine.worker import build_id


def _committed(repo: str) -> tuple[Path, str, Any]:
    """The repo as its default branch configures it, the same view schedules and runs use (so workflow IDs agree)."""
    root = Path(repo).expanduser().resolve()
    name, cfg = schedules.committed_config(root)
    if cfg is None:
        raise SystemExit(f"{root} is not opted in (no .factory.toml on its default branch)")
    return root, name, cfg


def team_ref(repo: str, team: str) -> TeamRef:
    root, name, _ = _committed(repo)
    return TeamRef(repo=str(root), repo_name=name, team=team)


def teams_for(repo: str, reason: str) -> list[str]:
    _, _, cfg = _committed(repo)
    return sorted(name for name, team in cfg.teams().items() if reason in team.wake_on)


async def cmd_signal(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    if a.reason == "decision" and not a.team:
        raise SystemExit("a decision goes back to one team: give --team")
    teams = [a.team] if a.team else teams_for(a.repo, a.reason)
    if not teams:
        print(f"no team in {a.repo} wakes on {a.reason}")
        return 0
    try:
        details = json.loads(a.details) if a.details else {}
        if not isinstance(details, dict) or not all(isinstance(v, str) for v in details.values()):
            raise ValueError
    except ValueError:
        raise SystemExit('--details must be a JSON object of strings, e.g. \'{"run": "123"}\'') from None
    for team in teams:
        ref = team_ref(a.repo, team)
        res = await wake(client, settings, WakeTeamInput(ref=ref, wake=Wake(reason=a.reason, event_id=a.event_id, details=details)))
        print(f"{ref.workflow_id}: {'woken' if res.woken else 'not woken — ' + res.reason}")
    return 0


async def cmd_run_now(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    ref = team_ref(a.repo, a.team)
    event = f"manual/{ref.workflow_id}/{uuid.uuid4()}"
    await wake(client, settings, WakeTeamInput(ref=ref, wake=Wake(reason="manual", event_id=event), bypass_pause=True))
    print(f"{ref.workflow_id}: woken by hand (runs even while its schedule is paused)")
    return 0


async def cmd_pause(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    ref = team_ref(a.repo, a.team)
    handle = client.get_schedule_handle(ref.schedule_id)
    if a.command == "pause":
        await handle.pause(note=a.note or "paused by hand")
    else:
        await handle.unpause(note=a.note or "resumed by hand")
    print(f"{ref.schedule_id}: {a.command}d")
    return 0


async def cmd_reconcile(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    for line in await schedules.reconcile(client, settings):
        print(line)
    return 0


async def cmd_status(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    for sid in sorted([s.id async for s in await client.list_schedules() if s.id.startswith(schedules.PREFIX)]):
        desc = await client.get_schedule_handle(sid).describe()
        state = desc.schedule.state
        nxt = desc.info.next_action_times[0].isoformat(timespec="minutes") if desc.info.next_action_times else "—"
        print(f"{sid}: {'PAUSED (' + (state.note or 'no note') + ')' if state.paused else 'active'}, next {nxt}")
    async for wf in client.list_workflows('WorkflowType = "TeamWorkflow" AND ExecutionStatus = "Running"'):
        print(f"{wf.id}: running since {wf.start_time.isoformat(timespec='minutes')}")
    async for wf in client.list_workflows('WorkflowType = "TeamWorkflow" AND ExecutionStatus != "Running"', limit=a.recent):
        line = f"{wf.id}: {wf.status.name.lower() if wf.status else '?'} {wf.close_time.isoformat(timespec='minutes') if wf.close_time else ''}"
        if wf.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
            line += " — continued with the wakes that arrived during the run"
        elif wf.status == WorkflowExecutionStatus.COMPLETED:
            try:
                result = await client.get_workflow_handle(wf.id, run_id=wf.run_id).result()
                line += f" — {result.get('outcome', '?')}: {str(result.get('summary', ''))[:120]}"
            except Exception as e:  # noqa: BLE001 — status is best effort, per line
                line += f" — result unreadable ({e})"
        elif wf.status is not None:
            line += f" — {await get_run_failure(client, wf.id, wf.run_id, 160)}"
        print(line)
    return 0


async def cmd_set_current(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    build = a.build_id or build_id()
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(namespace=NAMESPACE, deployment_name=DEPLOYMENT, build_id=build)
    )
    print(f"{DEPLOYMENT}: current build {build}")
    return 0


async def cmd_drained(client: Client, settings: EngineSettings, a: argparse.Namespace) -> int:
    """Exit 0 when the build's worker may stop: drained, unknown to the server, or never current and nothing pinned
    to it. Exit 1 while it is current or runs remain pinned to it."""
    version = WorkerDeploymentVersion(deployment_name=DEPLOYMENT, build_id=a.build_id)
    try:
        resp = await client.workflow_service.describe_worker_deployment_version(
            DescribeWorkerDeploymentVersionRequest(namespace=NAMESPACE, deployment_version=version._to_proto())
        )
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            print(f"{DEPLOYMENT} build {a.build_id}: unknown to the server (never ran)")
            return 0
        raise
    info = resp.worker_deployment_version_info
    status = info.drainage_info.status
    name = VersionDrainageStatus.Name(status).removeprefix("VERSION_DRAINAGE_STATUS_").lower()
    current = info.status == WorkerDeploymentVersionStatus.WORKER_DEPLOYMENT_VERSION_STATUS_CURRENT
    never_routed = status == VersionDrainageStatus.VERSION_DRAINAGE_STATUS_UNSPECIFIED and not info.HasField("current_since_time")
    print(f"{DEPLOYMENT} build {a.build_id}: {'current' if current else name}{' (never current)' if never_routed else ''}")
    return 0 if not current and (status == VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINED or never_routed) else 1


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
    sub.add_parser("build-id", help="this checkout's Build ID")
    c = sub.add_parser("set-current", help="route new runs to a Build ID")
    c.add_argument("--build-id")
    d = sub.add_parser("drained", help="exit 0 once nothing open is pinned to a Build ID")
    d.add_argument("--build-id", required=True)
    return p


COMMANDS = {
    "signal": cmd_signal, "run-now": cmd_run_now, "pause": cmd_pause, "resume": cmd_pause, "reconcile": cmd_reconcile,
    "status": cmd_status, "set-current": cmd_set_current, "drained": cmd_drained,
}


async def run(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if a.command == "build-id":
        print(build_id())
        return 0
    settings = settings_mod.load()
    client = await get_client(settings.temporal)
    return await COMMANDS[a.command](client, settings, a)


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
