"""Repo-side steps of a run, built on the factory's runner (sfactory/runner.py): read the team's config, prepare the
worktree, deliver the report, clean up. Each is safe to repeat."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engine import settings as settings_mod
from engine.factory import config, runner
from engine.models import ResolveInput, RunPaths, StepInput, StepResult, TeamPlan

NOT_OPTED_IN = "not opted in"


def run_env(cfg: Any, paths: RunPaths, run_name: str) -> dict[str, str]:
    """The environment a run's commands see: the caller's, scrubbed, plus the run's names (as `factory run` sets)."""
    env = {k: v for k, v in os.environ.items() if k not in runner.SCRUB}
    env.update(
        REPO=str(cfg.root), WORKTREE=paths.worktree, FACTORY_RUN=run_name, FACTORY_BRANCH=paths.branch,
        FACTORY_REPORT=runner.REPORT_REL, FACTORY_DEFAULT_BRANCH=cfg.default_branch,
    )
    return env


def _repo_config(repo: str) -> Any:
    root = Path(repo)
    if not (root / ".git").exists():
        raise ApplicationError(f"{repo} is not a git checkout", type="RepoMissing", non_retryable=True)
    return config.load(root)


@activity.defn(name="resolve_team")
async def resolve_team(inp: ResolveInput) -> TeamPlan:
    """The team as the repo's default branch configures it (not a possibly stale checkout), and the run's paths."""
    cfg = _repo_config(inp.ref.repo)
    with open(os.devnull, "w") as quiet:
        await asyncio.to_thread(
            runner._sh, ["git", "fetch", "--quiet", "origin", cfg.default_branch], cfg.root, dict(os.environ), quiet
        )
    try:
        rcfg = config.load_ref(cfg.root, f"origin/{cfg.default_branch}")
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


def _prepare(inp: StepInput) -> StepResult:
    cfg = _repo_config(inp.ref.repo)
    Path(inp.paths.worktree).parent.mkdir(parents=True, exist_ok=True)
    Path(inp.paths.log).parent.mkdir(parents=True, exist_ok=True)
    env = run_env(cfg, inp.paths, Path(inp.paths.worktree).name)
    with open(inp.paths.log, "a") as log:
        log.write(f"factory engine: {inp.ref.team} run in {inp.paths.worktree}\n")
        code, _ = runner.prepare_worktree(cfg, Path(inp.paths.worktree), inp.paths.branch, env, log)
    if code == runner._NOT_OPTED_IN:
        return StepResult(ok=False, message=f"{NOT_OPTED_IN}: no .factory.toml on {cfg.default_branch}")
    if code:
        return StepResult(ok=False, message=f"could not prepare the worktree (code {code}); log {inp.paths.log}")
    return StepResult(ok=True)


@activity.defn(name="prepare_worktree")
async def prepare_worktree(inp: StepInput) -> StepResult:
    task = asyncio.create_task(asyncio.to_thread(_prepare, inp))
    while not task.done():  # setup can take minutes: keep the heartbeat going so a dead worker is noticed
        activity.heartbeat()
        await asyncio.wait({task}, timeout=30)
    return task.result()


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
    """Remove the worktree and its local branches unless a commit would be lost; a missing worktree is success."""

    def go() -> StepResult:
        cfg = _repo_config(inp.ref.repo)
        Path(inp.paths.log).parent.mkdir(parents=True, exist_ok=True)
        with open(inp.paths.log, "a") as log:
            return StepResult(ok=True, message=runner.cleanup(cfg, Path(inp.paths.worktree), inp.paths.branch, log))

    return await asyncio.to_thread(go)
