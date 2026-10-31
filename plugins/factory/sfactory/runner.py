"""Run a factory capability unattended: a fresh worktree off the default branch, the repo's setup, then the agent.

The agent is whatever `[agent] command` says (Claude Code by default), so another client or a cloud runner is a
config change, not a code change. One run per repo and capability at a time (a lock file); every run keeps a log.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import subprocess
from pathlib import Path

from . import config as config_mod
from .config import CONFIG_NAME, Config

HOME = Path(os.environ.get("FACTORY_HOME", Path.home() / ".cache" / "software-factory"))

PROMPTS = {
    "upgrade": "/factory:upgrade",
}


def _sh(cmd: str | list[str], cwd: Path, env: dict, log) -> int:
    log.write(f"\n$ {cmd if isinstance(cmd, str) else shlex.join(cmd)}\n")
    log.flush()
    proc = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str), stdout=log, stderr=subprocess.STDOUT)
    return proc.returncode


def agent_argv(cfg: Config, prompt: str) -> list[str]:
    tools = ",".join(cfg.agent_allowed_tools)
    return [part.replace("{prompt}", prompt).replace("{allowed_tools}", tools) for part in cfg.agent_command]


def run(cfg: Config, capability: str, dry_run: bool = False, keep: bool = False) -> int:
    if capability not in PROMPTS:
        raise SystemExit(f"factory: unknown capability {capability!r} (known: {', '.join(PROMPTS)})")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    runs = HOME / "runs" / cfg.name
    logs = HOME / "logs" / cfg.name
    lock = runs / f"{capability}.lock"
    log_path = logs / f"{capability}-{stamp}.log"
    worktree = runs / f"{capability}-{stamp}"
    branch = f"factory/{capability}-{stamp}"
    argv = agent_argv(cfg, PROMPTS[capability])

    if dry_run:
        print(f"repo      {cfg.root}\nworktree  {worktree}\nbranch    {branch} (from origin/{cfg.default_branch})")
        print("setup     " + (" && ".join(cfg.worktree_setup) or "—"))
        print(f"agent     {shlex.join(argv)}\nlog       {log_path}")
        return 0
    runs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print(f"factory: a {capability} run for {cfg.name} is already going ({lock}); skipping")
        return 0

    env = {**os.environ, "REPO": str(cfg.root), "WORKTREE": str(worktree), "FACTORY_RUN": f"{capability}-{stamp}"}
    created = False
    try:
        with open(log_path, "w") as log:
            log.write(f"factory {capability} run {stamp} for {cfg.root}\n")
            code = _prepare_and_run(cfg, capability, worktree, branch, env, log, log_path)
            created = code != _NOT_CREATED
            if code == _NOT_CREATED:
                return 1
            if code == _NOT_OPTED_IN:
                return 0
        print(f"factory: {capability} run finished (agent exit {code}) — log {log_path}")
        return code
    finally:
        if created and not keep:
            pushed = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", branch], cwd=cfg.root, capture_output=True, text=True
            ).stdout.strip()
            if not pushed:  # nothing landed: leave no worktree or branch behind
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=cfg.root, capture_output=True)
                subprocess.run(["git", "branch", "-D", branch], cwd=cfg.root, capture_output=True)
        lock.unlink(missing_ok=True)


_NOT_CREATED = -1000
_NOT_OPTED_IN = -1001


def _prepare_and_run(cfg: Config, capability: str, worktree: Path, branch: str, env: dict, log, log_path: Path) -> int:
    """Fresh worktree off the default branch, its config and setup, then the agent. Returns the agent's exit code."""
    if _sh(["git", "fetch", "--quiet", "origin", cfg.default_branch], cfg.root, env, log) or _sh(
        ["git", "worktree", "add", "--quiet", "-b", branch, str(worktree), f"origin/{cfg.default_branch}"], cfg.root, env, log
    ):
        print(f"factory: could not create the run worktree — see {log_path}")
        return _NOT_CREATED
    # The run follows the default branch's config, not a possibly stale main checkout.
    if not (worktree / CONFIG_NAME).is_file():
        log.write(f"no {CONFIG_NAME} on origin/{cfg.default_branch}: repo not opted in\n")
        print(f"factory: {cfg.name} has no {CONFIG_NAME} on {cfg.default_branch} — not opted in; nothing to do")
        return _NOT_OPTED_IN
    wcfg = config_mod.load(worktree)
    for cmd in wcfg.worktree_setup:
        if _sh(cmd, worktree, env, log):
            print(f"factory: worktree setup `{cmd}` failed — see {log_path}")
            return 1
    return _sh(agent_argv(wcfg, PROMPTS[capability]), worktree, env, log)
