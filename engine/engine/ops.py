"""The engine's operations: one owner for each read and action, shared by `factory engine …` (engine/cli.py) and the
Factory Desk (docs/designs/desk.md, "Foundations first"). Functions return dataclasses and print nothing; a refusal
is an `OpError` with the reason in words.

Reads (`snapshot`) never run `git fetch`: team lists come from schedule IDs and each repo's already-fetched
origin/<default>. Actions fetch where they need the current config (reconcile, add a repo, resolve a team by path).
Every change is appended to `<state>/actions.jsonl`: time, who (`cli` or `desk`), action, target, before, after.
Git and file work inside an async action runs in a thread, so the desk's one event loop keeps serving pages.

A deploy is the CLI's own `factory engine upgrade` run from the source checkout; `start_deploy` starts it for the
desk (a build is frozen code, not a checkout, so the desk can't deploy in-process).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
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


def _installed() -> tuple[str | None, list[tuple[str, str | None]]]:
    """(the current build, each installed build with its commit), from disk."""
    return install.current_build(), [(b, install.build_commit(b)) for b in install.installed_builds()]


async def builds(client: Client) -> list[BuildView]:
    current, installed = await asyncio.to_thread(_installed)  # file reads: off the event loop
    out = []
    for build, commit in installed:
        try:
            server: BuildStatus | None = await build_status(client, build)
        except RPCError:
            server = None
        out.append(BuildView(build, commit, build == current, server))
    return out


async def snapshot(client: Client, settings: EngineSettings, recent: int = 10) -> EngineSnapshot:
    """Everything the engine is and does now: schedules, teams per repo, open and recent runs, builds, settings.
    Only Temporal calls run on the event loop; git and file reads run in a thread."""
    scheduled = await schedule_views(client)
    by_id = {s.schedule_id: s for s in scheduled}
    repos = await asyncio.to_thread(lambda: tuple(_repo_view(settings, entry, by_id) for entry in settings.repos))
    built = await builds(client)
    return EngineSnapshot(
        settings=settings,
        repos=repos,
        schedules=tuple(scheduled),
        open_runs=tuple(await open_runs(client)),
        recent_runs=tuple(await recent_runs(client, recent)),
        builds=tuple(built),
        current_build=next((b.build_id for b in built if b.current), None) or await asyncio.to_thread(install.current_build),
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
    changed = [line for line in report if line.startswith(("created ", "updated ", "deleted "))]  # unchanged: not listed
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


def _checked_add(path: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not (path.startswith("~") or Path(path).is_absolute()):
        raise OpError(f"{path}: give the repo's absolute path or a ~/… path (a relative path depends on the directory "
                      "the command or the desk happens to run in)")
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or _git_root(root) != root:
        raise OpError(f"{root} is not the top of a git checkout")
    _, name, _ = _committed(str(root), fetch=True)
    before, after = install.add_repos([path])
    return install.repo_entry(path), name, before, after


async def add_repo(client: Client, settings: EngineSettings, path: str, who: Who = "cli") -> RepoChange:
    """Add a repo whose default branch has a .factory.toml, then reconcile (its new schedules start paused)."""
    # git and the settings file in a thread: the desk's event loop serves other pages meanwhile
    entry, name, before, after = await asyncio.to_thread(_checked_add, path)
    report = await reconcile(client, replace(settings, repos=tuple(after.get("repos", ()))), who)
    change = log_action(settings, who, "add-repo", entry, before.get("repos", []), after.get("repos", []))
    return RepoChange(change, entry, name, report, unchanged=before == after)


async def remove_repo(client: Client, settings: EngineSettings, path: str, who: Who = "cli") -> RepoChange:
    """Remove a repo, then reconcile, which deletes its schedules. Its open runs finish; nothing new starts."""
    def checked() -> tuple[str, dict[str, Any], dict[str, Any]]:
        root = Path(path).expanduser().resolve()
        try:
            name = schedules.committed_config(root)[0]
        except Exception:  # noqa: BLE001 — a deleted or broken checkout can still be removed
            name = root.name
        try:
            before, after = install.remove_repo(path)
        except KeyError as e:
            raise OpError(e.args[0]) from e
        return name, before, after

    name, before, after = await asyncio.to_thread(checked)
    entry = next(r for r in before.get("repos", []) if r not in after.get("repos", []))
    report = await reconcile(client, replace(settings, repos=tuple(after.get("repos", ()))), who)
    change = log_action(settings, who, "remove-repo", entry, before.get("repos", []), after.get("repos", []))
    return RepoChange(change, entry, name, report, tuple(await open_runs(client, name)))


# ---- deploy (the desk's: `factory engine upgrade` from the source checkout) ---------------------------------------

DEPLOYS = "deploys"  # <state>/deploys/<ID>.log, and <ID>.json for what it was started with
DEPLOY_ID = re.compile(r"\d{8}T\d{6}Z")  # its UTC start time; the desk serves a deploy's log by this, never a path
EXIT_MARK = "factory desk: the upgrade exited with "  # the log's last line once the upgrade has ended
# The upgrade runs under sh so its log ends with its exit status although nothing waits for it (the desk restarts).
SPAWN_GRACE = 60  # seconds a just-started deploy counts as running before its upgrade holds the deploy lock
_starting = threading.Lock()  # one start_deploy at a time in this process (the desk's request threads)
DEPLOY_SCRIPT = f'"$1" engine upgrade; code=$?; printf "\\n{EXIT_MARK}%s\\n" "$code"; exit "$code"'


@dataclass(frozen=True)
class DeployPlan:
    """What a deploy from the desk would do: the source checkout's HEAD over the running build's commit."""

    source: str  # the checkout as recorded (`~/…`)
    head: str | None  # its HEAD; None when git can't tell
    branch: str | None
    build: str | None  # the running (current) build
    commit: str | None  # the commit it was made from
    busy: str | None  # a deploy, start or stop running now, in words

    @property
    def refusal(self) -> str:
        """Why it can't deploy now ("" when it can)."""
        if self.busy:
            return f"{self.busy} is running; deploy when it has finished"
        if self.head is None:
            return f"git can't read the source checkout {self.source}'s HEAD"
        if self.head == self.commit:
            return "the running build is already the source checkout's HEAD"
        return ""


@dataclass(frozen=True)
class DeployRun:
    deploy_id: str
    log: str
    state: Literal["running", "done", "failed", "unknown"]
    exit_code: int | None
    meta: dict[str, Any]  # source, head, branch, build, commit, when (epoch s) and how it was started


def _deploys(settings: EngineSettings) -> Path:
    return settings.state / DEPLOYS


def _ids(settings: EngineSettings) -> list[str]:
    """The desk's deploys, newest first."""
    return sorted((p.stem for p in _deploys(settings).glob("*.log") if DEPLOY_ID.fullmatch(p.stem)), reverse=True)


def read_deploy(settings: EngineSettings, deploy_id: str) -> DeployRun | None:
    """A deploy the desk started, by its ID (None for anything that isn't one): its log and whether it has ended."""
    if DEPLOY_ID.fullmatch(deploy_id) is None:
        return None
    try:
        text = (_deploys(settings) / f"{deploy_id}.log").read_text(errors="replace")
    except OSError:
        return None
    try:
        meta = json.loads((_deploys(settings) / f"{deploy_id}.json").read_text())
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    last = text.rstrip("\n").rpartition("\n")[2]
    code = last[len(EXIT_MARK):].strip() if last.startswith(EXIT_MARK) else ""
    if code.isdigit():
        return DeployRun(deploy_id, text, "done" if code == "0" else "failed", int(code), meta)
    # No exit status yet. Running is decided by the deploy lock (never a stored pid, which a reboot can hand to
    # another process): the newest deploy runs while the lock is held, or in its first SPAWN_GRACE seconds, before
    # the upgrade has taken the lock. Anything else ended without saying how (killed, or the machine went down).
    started = meta.get("started")
    fresh = isinstance(started, (int, float)) and 0 <= time.time() - started < SPAWN_GRACE
    newest = _ids(settings)[:1] == [deploy_id]
    running = newest and (fresh or install.deploy_running() is not None)
    return DeployRun(deploy_id, text, "running" if running else "unknown", None, meta)


def recent_deploys(settings: EngineSettings, limit: int = 5) -> list[DeployRun]:
    """The latest deploys the desk started, newest first."""
    return [d for d in (read_deploy(settings, i) for i in _ids(settings)[:limit]) if d is not None]


def deploy_plan(settings: EngineSettings) -> DeployPlan | None:
    """None before a deploy recorded the source checkout. Local reads only (git without a fetch, files)."""
    source = install.source_head()
    if source is None:
        return None
    build = install.current_build()
    busy = install.deploy_running()
    if busy is None:  # one the desk started that does not hold the lock yet
        busy = next((f"the deploy started {d.deploy_id}" for d in recent_deploys(settings, 1) if d.state == "running"),
                    None)
    return DeployPlan(source[0], source[1], source[2], build, install.build_commit(build) if build else None, busy)


def start_deploy(settings: EngineSettings, who: Who = "desk") -> DeployRun:
    """Deploy the source checkout's HEAD: `<source>/home/bin/factory engine upgrade`, exactly what the CLI runs,
    detached so it outlives the desk (the upgrade restarts the desk: `install.spawn_detached`), with its output in
    `<state>/deploys/<ID>.log`. Refused while a deploy runs; the upgrade also takes the deploy lock itself. One at a
    time in this process, so two requests at once can't both pass the check."""
    with _starting:
        return _start_deploy(settings, who)


def _start_deploy(settings: EngineSettings, who: Who) -> DeployRun:
    plan = deploy_plan(settings)
    if plan is None:
        raise OpError("no deploy has recorded the source checkout yet: run `factory engine upgrade` in it once")
    if plan.refusal:
        raise OpError(plan.refusal)
    checkout = Path(plan.source).expanduser()
    factory = checkout / "home" / "bin" / "factory"
    if not os.access(factory, os.X_OK):
        raise OpError(f"{factory} is missing or not executable")
    folder = _deploys(settings)
    folder.mkdir(parents=True, exist_ok=True)
    deploy_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        fd = os.open(folder / f"{deploy_id}.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise OpError("a deploy started this very second; reload the engine page to see it") from None
    log = folder / f"{deploy_id}.log"
    with os.fdopen(fd, "w") as out:
        out.write(f"factory desk: deploying {plan.source} at {plan.head} (branch {plan.branch or 'detached'}) over "
                  f"build {plan.build or 'none'} from commit {plan.commit or 'unknown'}\n")
    meta: dict[str, Any] = {"source": plan.source, "head": plan.head, "branch": plan.branch, "build": plan.build,
                            "commit": plan.commit, "started": time.time()}
    install.write_atomic(folder / f"{deploy_id}.json", json.dumps(meta) + "\n", mode=0o600)
    argv = ["sh", "-c", DEPLOY_SCRIPT, "factory-deploy", str(factory)]
    try:
        meta["how"] = install.spawn_detached(f"factory-deploy-{deploy_id}", argv, checkout, log)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        with open(log, "a") as out:
            out.write(f"\n{EXIT_MARK}127\n")  # it never ran: the page says failed, not running
        raise OpError(f"the upgrade could not be started: {e}") from None
    install.write_atomic(folder / f"{deploy_id}.json", json.dumps(meta) + "\n", mode=0o600)
    log_action(settings, who, "deploy", plan.source, plan.commit, plan.head)
    return DeployRun(deploy_id, "", "running", None, meta)


