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

from .config import Config

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
    try:
        with open(log_path, "w") as log:
            log.write(f"factory {capability} run {stamp} for {cfg.root}\n")
            steps = [
                ["git", "fetch", "--quiet", "origin", cfg.default_branch],
                ["git", "worktree", "add", "--quiet", "-b", branch, str(worktree), f"origin/{cfg.default_branch}"],
            ]
            for step in steps:
                if _sh(step, cfg.root, env, log):
                    print(f"factory: {shlex.join(step)} failed — see {log_path}")
                    return 1
            for cmd in cfg.worktree_setup:
                if _sh(cmd, worktree, env, log):
                    print(f"factory: worktree setup `{cmd}` failed — see {log_path}")
                    return 1
            code = _sh(argv, worktree, env, log)
        pushed = subprocess.run(["git", "ls-remote", "--heads", "origin", branch], cwd=cfg.root, capture_output=True, text=True).stdout.strip()
        if not pushed and not keep:  # nothing landed: leave no worktree or branch behind
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=cfg.root, capture_output=True)
            subprocess.run(["git", "branch", "-D", branch], cwd=cfg.root, capture_output=True)
        print(f"factory: {capability} run finished (agent exit {code}) — log {log_path}")
        return code
    finally:
        lock.unlink(missing_ok=True)
