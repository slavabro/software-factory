"""Repo-side steps of a run, built on the factory's runner (sfactory/runner.py): read the team's config, prepare the
worktree, deliver the report, clean up. Each is safe to repeat, and none blocks the worker's event loop."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engine import process, steps
from engine import settings as settings_mod
from engine.factory import config, runner
from engine.models import ResolveInput, RunPaths, StepInput, StepResult, TeamPlan

NOT_OPTED_IN = steps.NOT_OPTED_IN
HEARTBEAT_EVERY = 30.0


def run_env(cfg: Any, paths: RunPaths, run_name: str) -> dict[str, str]:
    return steps.run_env(cfg, paths, run_name)


def _repo_config(repo: str) -> Any:
    root = Path(repo)
    if not (root / ".git").exists():
        raise ApplicationError(f"{repo} is not a git checkout", type="RepoMissing", non_retryable=True)
    try:
        return config.load(root)
    except config.ConfigError as e:
        raise ApplicationError(str(e), type="ConfigInvalid", non_retryable=True) from None


@activity.defn(name="resolve_team")
async def resolve_team(inp: ResolveInput) -> TeamPlan:
    """The team as the repo's default branch configures it (not a possibly stale checkout), and the run's paths."""

    def go() -> TeamPlan:
        cfg = _repo_config(inp.ref.repo)
        subprocess.run(["git", "fetch", "--quiet", "origin", cfg.default_branch], cwd=cfg.root, capture_output=True,
                       timeout=90)
        ref = f"origin/{cfg.default_branch}"
        if not config.committed(cfg.root, ref):
            return TeamPlan(enabled=False, reason=f"{NOT_OPTED_IN}: no .factory.toml on {cfg.default_branch}")
        try:
            rcfg = config.load_ref(cfg.root, ref)
        except config.ConfigError as e:
            raise ApplicationError(str(e), type="ConfigInvalid", non_retryable=True) from None
        team = rcfg.teams().get(inp.ref.team)
        if team is None:
            return TeamPlan(enabled=False, reason=f"no team {inp.ref.team!r} in {cfg.name}'s .factory.toml")
        suffix = f"{inp.day}-{inp.run_id[:8]}"
        state = settings_mod.load().state
        logs = state / "logs" / inp.ref.repo_name / inp.ref.team
        paths = RunPaths(
            worktree=str(state / "worktrees" / inp.ref.repo_name / inp.ref.team / suffix),
            branch=f"factory/{inp.ref.team}/{suffix}",
            log=str(logs / f"{suffix}.log"),
            report=str(logs / f"{suffix}.report.md"),
        )
        return TeamPlan(enabled=True, prompt=team.prompt, budget_minutes=team.budget_minutes, paths=paths)

    return await asyncio.to_thread(go)


@activity.defn(name="prepare_worktree")
async def prepare_worktree(inp: StepInput) -> StepResult:
    """The worktree and the repo's setup, in a child process (killed as a group on cancel). A result a retry can't
    change (not opted in, invalid config) comes back as ok=False; anything else that failed raises, to be retried."""
    await asyncio.to_thread(process.kill_stale, Path(inp.paths.worktree))  # an agent a dead worker left in it
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "engine.steps", "prepare",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    task = asyncio.ensure_future(proc.communicate(inp.model_dump_json().encode()))
    try:
        while not task.done():
            activity.heartbeat()
            await asyncio.wait({task}, timeout=HEARTBEAT_EVERY)
    except asyncio.CancelledError:
        await asyncio.to_thread(process.kill_group, proc.pid)
        task.cancel()
        raise
    out, err = task.result()
    if proc.returncode:
        raise ApplicationError(f"prepare crashed: {err.decode()[-1000:]}", type="PrepareCrashed")
    result = json.loads(out.decode().strip().splitlines()[-1])
    code, message = int(result["code"]), str(result["message"])
    if code == 0:
        return StepResult(ok=True)
    if code in steps.FINAL:
        return StepResult(ok=False, message=message)
    raise ApplicationError(message, type="PrepareFailed")  # retried (network, a flaky setup)


@activity.defn(name="deliver_report")
async def deliver_report(inp: StepInput) -> StepResult:
    """The agent's digest (its report file, else its last message), sent with the repo's `[report] with`."""

    def go() -> StepResult:
        cfg = _repo_config(inp.ref.repo)
        log, report = Path(inp.paths.log), Path(inp.paths.report)
        runner.collect_report(Path(inp.paths.worktree), log, report)
        if not report.is_file():
            return StepResult(ok=True, message="the run wrote no report")
        if report.stat().st_size:
            runner.deliver(cfg, report, Path(inp.paths.worktree), run_env(cfg, inp.paths, log.stem), log)
        return StepResult(ok=True, message=f"report {report}")

    return await asyncio.to_thread(go)


@activity.defn(name="remove_worktree")
async def remove_worktree(inp: StepInput) -> StepResult:
    """Stop any agent still in the worktree, then remove it and its local branches unless a commit would be lost; a
    missing worktree is success."""

    def go() -> StepResult:
        cfg = _repo_config(inp.ref.repo)
        stale = process.kill_stale(Path(inp.paths.worktree))
        Path(inp.paths.log).parent.mkdir(parents=True, exist_ok=True)
        with open(inp.paths.log, "a") as log:
            if stale:
                log.write(f"\nfactory engine: stopped an agent still running in the worktree (pid {stale})\n")
            return StepResult(ok=True, message=runner.cleanup(cfg, Path(inp.paths.worktree), inp.paths.branch, log))

    return await asyncio.to_thread(go)
