"""The engine's operations: one owner for each read and action, shared by `factory engine …` (engine/cli.py) and the
Factory Desk (docs/designs/desk.md, "Foundations first"). Functions return dataclasses and print nothing; a refusal
is an `OpError` with the reason in words.

Reads (`snapshot`) never run `git fetch`: team lists come from schedule IDs and each repo's already-fetched
origin/<default>. Actions fetch where they need the current config (reconcile, add a repo, resolve a team by path).
Every change is appended to `<state>/actions.jsonl`: time, who (`cli` or `desk`), action, target, before, after.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from temporalio.api.enums.v1 import VersionDrainageStatus, WorkerDeploymentVersionStatus
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus
from temporalio.common import WorkerDeploymentVersion
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.temporal import get_run_failure
from engine import schedules
from engine.activities.teams import failures_in_a_row, wake
from engine.factory import install
from engine.factory import status as words
from engine.models import TeamRef, Wake, WakeTeamInput, WakeTeamResult
from engine.settings import DEPLOYMENT, NAMESPACE, TASK_QUEUE, EngineSettings

Who = Literal["cli", "desk"]
ACTIONS_LOG = "actions.jsonl"
FAILURE_CHARS = 500
FAILED_WORDS = {"failed": "Failed", "terminated": "Terminated", "canceled": "Cancelled", "timed_out": "Timed out"}


class OpError(Exception):
    """An operation refused, with the reason for the principal (not opted in, no such schedule, …)."""


# ---- what a snapshot holds ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduleView:
    schedule_id: str
    repo_name: str
    team: str
    paused: bool
    note: str
    next_run: datetime | None


@dataclass(frozen=True)
class TeamView:
    ref: TeamRef
    schedule: str  # the cron in .factory.toml, "" or "off" when it runs on events only
    wake_on: tuple[str, ...]
    failures: int  # failed runs in a row (state/teams/<repo>/<team>/state.json)
    scheduled: ScheduleView | None  # its Temporal Schedule, None when it has none (yet)
    prompt: str = ""  # what its agent session is asked to do (what the team does)
    budget_minutes: int = 0


@dataclass(frozen=True)
class RepoView:
    entry: str  # as engine.toml has it
    root: str
    name: str
    teams: tuple[TeamView, ...] = ()
    problem: str = ""  # why its teams can't be listed (not opted in, origin/<default> missing, a broken config)


@dataclass(frozen=True)
class RunView:
    workflow_id: str
    run_id: str
    repo_name: str
    team: str
    status: str  # running, completed, failed, terminated, canceled, timed_out, continued_as_new
    started: datetime | None
    closed: datetime | None
    outcome: str | None = None  # a completed run's outcome: done, failed, interrupted, skipped
    summary: str = ""
    failure: str | None = None  # why it did not complete, or why its result could not be read
    reasons: tuple[str, ...] = ()  # who woke it (a completed run's result says; older runs and open ones don't)

    @property
    def state(self) -> tuple[str, str]:
        """(status key, word) in the shared status words (sfactory/status.py): how the CLI and the desk say it."""
        if self.status == "running":
            return "running", words.status("running").word
        if self.status == "continued_as_new":
            return "continued", words.status("continued").word
        if self.outcome is not None:
            return self.outcome, words.status(self.outcome).word
        if self.status == "completed":  # its result could not be read
            return "unknown", "Completed"
        return "failed", FAILED_WORDS.get(self.status, self.status.replace("_", " ").capitalize())


@dataclass(frozen=True)
class BuildStatus:
    build_id: str
    known: bool  # the server has seen a worker of this build
    current: bool
    drainage: str  # the server's word: draining, drained, unspecified
    never_current: bool

    @property
    def may_stop(self) -> bool:
        """Its worker may stop: not current, and drained, never routed to, or unknown to the server."""
        return not self.known or (not self.current and (self.drainage == "drained" or self.never_current))


@dataclass(frozen=True)
class BuildView:
    build_id: str
    commit: str | None  # the commit it was snapshotted from
    current: bool  # this machine's `current` build
    server: BuildStatus | None  # None when the server could not say


@dataclass(frozen=True)
class EngineSnapshot:
    settings: EngineSettings
    repos: tuple[RepoView, ...]
    schedules: tuple[ScheduleView, ...]  # every factory schedule, including any no configured repo claims
    open_runs: tuple[RunView, ...]
    recent_runs: tuple[RunView, ...]
    builds: tuple[BuildView, ...]
    current_build: str | None


@dataclass(frozen=True)
class Change:
    """One line of the action log."""

    time: str
    who: str
    action: str
    target: str
    before: Any
    after: Any


@dataclass(frozen=True)
class RunNow:
    change: Change
    ref: TeamRef
    unattended: bool  # False: the spend switch is off, so the run stops before it starts a session


@dataclass(frozen=True)
class RepoChange:
    change: Change
    entry: str
    name: str
    report: list[str]  # what reconcile did
    open_runs: tuple[RunView, ...] = ()  # a removed repo's runs still open: they finish, nothing new starts
    unchanged: bool = False  # the repo was already in (add) — nothing written


@dataclass(frozen=True)
class Signalled:
    ref: TeamRef
    result: WakeTeamResult


# ---- the action log -----------------------------------------------------------------------------------------------

def log_action(settings: EngineSettings, who: Who, action: str, target: str, before: Any, after: Any) -> Change:
    """Append the change to the log; a no-op (before equals after) is returned but not logged."""
    change = Change(datetime.now(timezone.utc).isoformat(timespec="seconds"), who, action, target, before, after)
    if before == after:
        return change
    settings.state.mkdir(parents=True, exist_ok=True)
    with open(settings.state / ACTIONS_LOG, "a") as log:  # one write per line: appends never interleave
        log.write(json.dumps(asdict(change), default=str) + "\n")
    return change


def read_actions(settings: EngineSettings) -> list[Change]:
    """The log's changes, oldest first; a line that isn't a whole change (a torn write, a hand edit) is skipped."""
    path = settings.state / ACTIONS_LOG
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(Change(**json.loads(line)))
        except (ValueError, TypeError):
            continue
    return out


# ---- teams and repos ----------------------------------------------------------------------------------------------

def split_id(kind_id: str) -> tuple[str, str]:
    """(repo name, team) from `schedule/<repo>/<team>` or `team/<repo>/<team>`; a repo name may hold a slash."""
    rest = kind_id.split("/", 1)[1] if "/" in kind_id else kind_id
    repo, _, team = rest.rpartition("/")
    return repo, team


def _committed(repo: str, fetch: bool) -> tuple[Path, str, Any]:
    """(root, name, config) as the repo's default branch configures it, the same view schedules and runs use (so
    workflow IDs agree); `fetch` first brings that branch up to date."""
    root = Path(repo).expanduser().resolve()
    try:
        if fetch:
            schedules.fetch_default(root)
        name, cfg = schedules.committed_config(root)
    except Exception as e:  # noqa: BLE001 — a missing checkout, a broken config: say which
        raise OpError(f"{root}: {e}") from e
    if cfg is None:
        raise OpError(f"{root} is not opted in (no .factory.toml on its default branch)")
    return root, name, cfg


def team_ref(repo: str, team: str, fetch: bool = True) -> TeamRef:
    """A team by its repo's path (for the CLI; the desk has refs from its snapshot)."""
    root, name, cfg = _committed(repo, fetch)
    teams = cfg.teams()
    if team not in teams:
        raise OpError(f"{name} has no team {team} (its teams: {', '.join(sorted(teams)) or 'none'})")
    return TeamRef(repo=str(root), repo_name=name, team=team)


def _repo_view(settings: EngineSettings, entry: str, scheduled: dict[str, ScheduleView]) -> RepoView:
    root = Path(entry).expanduser().resolve()
    try:
        name, cfg = schedules.committed_config(root)
    except Exception as e:  # noqa: BLE001 — one repo's problem must not fail the page
        return RepoView(entry, str(root), root.name, problem=str(e)[:200])
    if cfg is None:
        return RepoView(entry, str(root), name, problem="not opted in: no .factory.toml on its default branch")
    teams = []
    for team, spec in sorted(cfg.teams().items()):  # type: ignore[attr-defined]
        ref = TeamRef(repo=str(root), repo_name=name, team=team)
        teams.append(TeamView(ref, spec.schedule or "", tuple(spec.wake_on), failures_in_a_row(settings, ref),
                              scheduled.get(ref.schedule_id), prompt=spec.prompt, budget_minutes=spec.budget_minutes))
    return RepoView(entry, str(root), name, tuple(teams))


def find_team(settings: EngineSettings, workflow_id: str) -> TeamView | None:
    """A team of a configured repo by its ID (`team/<repo>/<team>`), as its default branch configures it; None for
    any other ID. Local reads only, like `snapshot`: the desk resolves IDs from the client this way, never paths."""
    repo_name, team = split_id(workflow_id)
    if not workflow_id.startswith("team/") or not repo_name or not team:
        return None
    for entry in settings.repos:
        view = _repo_view(settings, entry, {})
        if view.name == repo_name:
            return next((t for t in view.teams if t.ref.team == team), None)
    return None


# ---- reads --------------------------------------------------------------------------------------------------------

async def schedule_views(client: Client) -> list[ScheduleView]:
    out = []
    ids = sorted([s.id async for s in await client.list_schedules() if s.id.startswith(schedules.PREFIX)])
    for sid in ids:
        try:
            desc = await client.get_schedule_handle(sid).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:  # deleted between the list and now
                continue
            raise
        state = desc.schedule.state
        repo, team = split_id(sid)
        nxt = desc.info.next_action_times[0] if desc.info.next_action_times else None
        out.append(ScheduleView(sid, repo, team, state.paused, state.note or "", nxt))
    return out


def _status_word(status: WorkflowExecutionStatus | None) -> str:
    return status.name.lower() if status else "unknown"


async def _run_view(client: Client, wf: WorkflowExecution) -> RunView:
    repo, team = split_id(wf.id)
    base = RunView(wf.id, wf.run_id, repo, team, _status_word(wf.status), wf.start_time, wf.close_time)
    if wf.status == WorkflowExecutionStatus.COMPLETED:
        try:
            result = await client.get_workflow_handle(wf.id, run_id=wf.run_id).result()
            return replace(base, outcome=str(result.get("outcome", "?")), summary=str(result.get("summary", "")),
                           reasons=tuple(str(r) for r in result.get("reasons", ())))
        except Exception as e:  # noqa: BLE001 — per run: one unreadable result must not hide the rest
            return replace(base, failure=f"result unreadable ({e})")
    if wf.status in (WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.CONTINUED_AS_NEW) or wf.status is None:
        return base
    return replace(base, failure=await get_run_failure(client, wf.id, wf.run_id, FAILURE_CHARS))


async def open_runs(client: Client, repo_name: str | None = None) -> list[RunView]:
    runs = [await _run_view(client, wf)
            async for wf in client.list_workflows('WorkflowType = "TeamWorkflow" AND ExecutionStatus = "Running"')]
    return [r for r in runs if repo_name is None or r.repo_name == repo_name]


def _quoted(value: str) -> str:
    """A string literal in a visibility query."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def recent_runs(client: Client, limit: int, workflow_id: str | None = None) -> list[RunView]:
    """The latest closed team runs, newest first (of one team with `workflow_id`). Each completed run costs a
    result read, so callers keep `limit` small."""
    if limit <= 0:
        return []
    query = 'WorkflowType = "TeamWorkflow" AND ExecutionStatus != "Running"'
    if workflow_id is not None:
        query += f" AND WorkflowId = {_quoted(workflow_id)}"
    return [await _run_view(client, wf) async for wf in client.list_workflows(query, limit=limit)]


async def pollers(client: Client) -> dict[str, datetime]:
    """Each build whose worker polls the task queue now (as the server last saw it) → when it last polled."""
    resp = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
        namespace=NAMESPACE, task_queue=TaskQueue(name=TASK_QUEUE), task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
    ))
    out: dict[str, datetime] = {}
    for p in resp.pollers:
        build = p.deployment_options.build_id or p.worker_version_capabilities.build_id or "unversioned"
        seen = p.last_access_time.ToDatetime(tzinfo=timezone.utc)
        out[build] = max(seen, out.get(build, seen))
    return out


async def build_status(client: Client, build: str) -> BuildStatus:
    version = WorkerDeploymentVersion(deployment_name=DEPLOYMENT, build_id=build)
    try:
        resp = await client.workflow_service.describe_worker_deployment_version(
            DescribeWorkerDeploymentVersionRequest(namespace=NAMESPACE, deployment_version=version._to_proto())
        )
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return BuildStatus(build, known=False, current=False, drainage="unknown", never_current=True)
        raise
    info = resp.worker_deployment_version_info
    status = info.drainage_info.status
    return BuildStatus(
        build,
        known=True,
        current=info.status == WorkerDeploymentVersionStatus.WORKER_DEPLOYMENT_VERSION_STATUS_CURRENT,
        drainage=VersionDrainageStatus.Name(status).removeprefix("VERSION_DRAINAGE_STATUS_").lower(),
        never_current=status == VersionDrainageStatus.VERSION_DRAINAGE_STATUS_UNSPECIFIED
        and not info.HasField("current_since_time"),
    )


async def builds(client: Client) -> list[BuildView]:
    current = install.current_build()
    out = []
    for build in install.installed_builds():
        try:
            server: BuildStatus | None = await build_status(client, build)
        except RPCError:
            server = None
        out.append(BuildView(build, install.build_commit(build), build == current, server))
    return out


async def snapshot(client: Client, settings: EngineSettings, recent: int = 10) -> EngineSnapshot:
    """Everything the engine is and does now: schedules, teams per repo, open and recent runs, builds, settings."""
    scheduled = await schedule_views(client)
    by_id = {s.schedule_id: s for s in scheduled}
    return EngineSnapshot(
        settings=settings,
        repos=tuple(_repo_view(settings, entry, by_id) for entry in settings.repos),
        schedules=tuple(scheduled),
        open_runs=tuple(await open_runs(client)),
        recent_runs=tuple(await recent_runs(client, recent)),
        builds=tuple(await builds(client)),
        current_build=install.current_build(),
    )


# ---- actions ------------------------------------------------------------------------------------------------------

async def _schedule_state(client: Client, ref: TeamRef) -> dict[str, Any]:
    try:
        desc = await client.get_schedule_handle(ref.schedule_id).describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise OpError(f"{ref.schedule_id}: no such schedule (the team runs on events only, or reconcile has not "
                          "run yet)") from e
        raise
    return {"paused": desc.schedule.state.paused, "note": desc.schedule.state.note or ""}


async def pause(client: Client, settings: EngineSettings, ref: TeamRef, note: str | None = None,
                who: Who = "cli") -> Change:
    before = await _schedule_state(client, ref)
    note = note or "paused by hand"
    await client.get_schedule_handle(ref.schedule_id).pause(note=note)
    return log_action(settings, who, "pause", ref.schedule_id, before, {"paused": True, "note": note})


async def resume(client: Client, settings: EngineSettings, ref: TeamRef, note: str | None = None,
                 who: Who = "cli") -> Change:
    before = await _schedule_state(client, ref)
    note = note or "resumed by hand"
    await client.get_schedule_handle(ref.schedule_id).unpause(note=note)
    return log_action(settings, who, "resume", ref.schedule_id, before, {"paused": False, "note": note})


async def run_now(client: Client, settings: EngineSettings, ref: TeamRef, who: Who = "cli") -> RunNow:
    """Wake the team by hand, past its schedule's pause. With the spend switch off the run stops before a session."""
    # The switch as the run will read it, read before the wake so a broken file stops here, not after a run started.
    unattended = bool(install.settings().get("unattended", EngineSettings.unattended))
    event = f"manual/{ref.workflow_id}/{uuid.uuid4()}"
    await wake(client, settings, WakeTeamInput(ref=ref, wake=Wake(reason="manual", event_id=event), bypass_pause=True))
    change = log_action(settings, who, "run-now", ref.workflow_id, None, {"event_id": event})
    return RunNow(change, ref, unattended)


async def signal(client: Client, settings: EngineSettings, repo: str, reason: str, event_id: str,
                 details: dict[str, str] | None = None, team: str | None = None) -> list[Signalled]:
    """Wake the teams that listen for `reason` (or the one `team`); a paused team is not woken. An event, not a
    change by the principal, so not in the action log."""
    if reason == "decision" and not team:
        raise OpError("a decision goes back to one team: give --team")
    if team:
        refs = [team_ref(repo, team)]
    else:
        root, name, cfg = _committed(repo, fetch=True)
        refs = [TeamRef(repo=str(root), repo_name=name, team=n)
                for n, spec in sorted(cfg.teams().items()) if reason in spec.wake_on]
    out = []
    for ref in refs:
        wake_ = Wake(reason=reason, event_id=event_id, details=details or {})  # type: ignore[arg-type]
        out.append(Signalled(ref, await wake(client, settings, WakeTeamInput(ref=ref, wake=wake_))))
    return out


async def reconcile(client: Client, settings: EngineSettings, who: Who = "cli") -> list[str]:
    report = await schedules.reconcile(client, settings)
    changed = [line for line in report if not line.startswith("updated ")]  # an update to the same spec is no change
    if changed:
        log_action(settings, who, "reconcile", "schedules", None, changed)
    return report


async def set_current(client: Client, settings: EngineSettings, build: str, who: Who = "cli") -> Change:
    """Route new runs to `build` (Worker Deployment `factory-engine`); open runs stay on the build they started on."""
    resp = await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(namespace=NAMESPACE, deployment_name=DEPLOYMENT, build_id=build)
    )
    before = resp.previous_deployment_version.build_id or None
    return log_action(settings, who, "set-current", DEPLOYMENT, before, build)


def set_unattended(settings: EngineSettings, on: bool, who: Who = "cli") -> Change:
    """The spend switch. Off stops runs that have not started a session; a session already running finishes."""
    before, after = install.set_setting("unattended", on)
    return log_action(settings, who, "spend", "unattended", before.get("unattended", EngineSettings.unattended),
                      after["unattended"])


def _git_root(path: Path) -> Path | None:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True)
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 else None


async def add_repo(client: Client, settings: EngineSettings, path: str, who: Who = "cli") -> RepoChange:
    """Add a repo whose default branch has a .factory.toml, then reconcile (its new schedules start paused)."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or _git_root(root) != root:
        raise OpError(f"{root} is not the top of a git checkout")
    _, name, _ = _committed(str(root), fetch=True)
    entry = install.repo_entry(path)
    before, after = install.add_repos([path])
    report = await reconcile(client, replace(settings, repos=tuple(after.get("repos", ()))), who)
    change = log_action(settings, who, "add-repo", entry, before.get("repos", []), after.get("repos", []))
    return RepoChange(change, entry, name, report, unchanged=before == after)


async def remove_repo(client: Client, settings: EngineSettings, path: str, who: Who = "cli") -> RepoChange:
    """Remove a repo, then reconcile, which deletes its schedules. Its open runs finish; nothing new starts."""
    root = Path(path).expanduser().resolve()
    try:
        name = schedules.committed_config(root)[0]
    except Exception:  # noqa: BLE001 — a deleted or broken checkout can still be removed
        name = root.name
    try:
        before, after = install.remove_repo(path)
    except KeyError as e:
        raise OpError(e.args[0]) from e
    entry = next(r for r in before.get("repos", []) if r not in after.get("repos", []))
    report = await reconcile(client, replace(settings, repos=tuple(after.get("repos", ()))), who)
    change = log_action(settings, who, "remove-repo", entry, before.get("repos", []), after.get("repos", []))
    return RepoChange(change, entry, name, report, tuple(await open_runs(client, name)))


