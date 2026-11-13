"""Run a factory capability unattended: a fresh worktree off the default branch, the repo's setup, then the agent.

The agent is whatever `[agent] command` says (Claude Code by default), so another client or a cloud runner is a
config change, not a code change. Safety: one run per repo and capability (a PID lock a dead run can't hold
forever), a timeout that kills the agent's whole process group, a scrubbed environment, and cleanup that never
deletes unpushed commits.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config as config_mod
from .config import CONFIG_NAME, Config

HOME = Path(os.environ.get("FACTORY_HOME", Path.home() / ".cache" / "software-factory"))
PRUNE_DAYS = 30
SCRUB = ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME", "CLAUDECODE")  # the caller's interpreter setup is not the run's

PROMPTS = {
    "upgrade": "/factory:upgrade",
}

# The agent writes its digest inside its own worktree (a path outside it is not writable in an unattended run);
# git ignores it there, and the runner copies it out before cleanup.
REPORT_REL = ".factory/report.md"

_NOT_CREATED = -1000
_NOT_OPTED_IN = -1001
_INVALID_CONFIG = -1002
_SETUP_FAILED = -1003
_TIMED_OUT = 124


def _sh(cmd: str | list[str], cwd: Path, env: dict, log, timeout: float | None = None) -> int:
    log.write(f"\n$ {cmd if isinstance(cmd, str) else shlex.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, shell=isinstance(cmd, str), stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        log.write(f"\nfactory: timed out after {timeout:.0f}s; process group killed\n")
        return _TIMED_OUT
    except BaseException:
        os.killpg(proc.pid, signal.SIGTERM)  # our own SIGTERM/^C: take the agent down with us
        raise


def agent_argv(cfg: Config, prompt: str) -> list[str]:
    fill = {
        "{prompt}": prompt,
        "{allowed_tools}": ",".join(cfg.agent_allowed_tools),
        "{disallowed_tools}": ",".join(cfg.agent_disallowed_tools),
    }
    out = []
    for part in cfg.agent_command:
        for k, v in fill.items():
            part = part.replace(k, v)
        out.append(part.replace("{default_branch}", cfg.default_branch))
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire(lock: Path) -> bool:
    """Take the lock; take over one left by a dead process."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _alive(pid):
                return False
            lock.unlink(missing_ok=True)  # stale: its run died without cleaning up
    return False


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def cleanup(cfg: Config, worktree: Path, prefix: str, log=None) -> str:
    """Remove the run's worktree and local branches unless a commit would be lost. Returns what it did."""
    remote = {
        line.split("refs/heads/", 1)[1]
        for line in _git(["ls-remote", "--heads", "origin", f"{prefix}*"], cfg.root).splitlines()
        if "refs/heads/" in line
    }
    local = [b.strip("* +") for b in _git(["branch", "--list", f"{prefix}*"], cfg.root).splitlines() if b.strip()]
    unpushed = []
    for b in local:
        base = f"origin/{b}" if b in remote else f"origin/{cfg.default_branch}"
        if _git(["rev-list", "--count", f"{base}..{b}"], cfg.root) not in ("", "0"):
            unpushed.append(b)
    dirty = worktree.is_dir() and bool(_git(["status", "--porcelain"], worktree))
    if unpushed or dirty:
        msg = f"kept {worktree}: unpushed commits on {', '.join(unpushed) or '—'}{' and uncommitted changes' if dirty else ''}"
    else:
        if worktree.is_dir():
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=cfg.root, capture_output=True)
        for b in local:
            subprocess.run(["git", "branch", "-D", b], cwd=cfg.root, capture_output=True)
        msg = f"cleaned up {worktree.name} ({len(remote)} branch(es) pushed)"
    if log:
        log.write(f"\nfactory: {msg}\n")
    return msg


def prune(cfg: Config, runs: Path, days: int = PRUNE_DAYS) -> None:
    """Old run dirs whose work is all pushed go; anything with unpushed commits stays."""
    cutoff = time.time() - days * 86400
    for d in runs.glob("*-20*"):
        if d.is_dir() and d.stat().st_mtime < cutoff:
            cleanup(cfg, d, f"factory/{d.name}")
    subprocess.run(["git", "worktree", "prune"], cwd=cfg.root, capture_output=True)


def _raise_exit(*_):
    raise SystemExit(143)


def run(cfg: Config, capability: str, dry_run: bool = False, keep: bool = False) -> int:
    if capability not in PROMPTS:
        raise SystemExit(f"factory: unknown capability {capability!r} (known: {', '.join(PROMPTS)})")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    runs = HOME / "runs" / cfg.name
    logs = HOME / "logs" / cfg.name
    lock = runs / f"{capability}.lock"
    log_path = logs / f"{capability}-{stamp}.log"
    report = logs / f"{capability}-{stamp}.report.md"
    worktree = runs / f"{capability}-{stamp}"
    branch = f"factory/{capability}-{stamp}"

    if dry_run:
        # Show what the run will use: the config on origin/<default_branch> (as last fetched), not the checkout's.
        ref = f"origin/{cfg.default_branch}"
        rcfg = config_mod.load_ref(cfg.root, ref)
        opted_in = subprocess.run(["git", "cat-file", "-e", f"{ref}:{CONFIG_NAME}"], cwd=cfg.root, capture_output=True).returncode == 0
        print(f"repo      {cfg.root}\nworktree  {worktree}\nbranch    {branch} (from {ref})")
        print(f"config    {ref}:{CONFIG_NAME}" + ("" if opted_in else " — missing: not opted in, a run does nothing"))
        print("setup     " + (" && ".join(rcfg.worktree_setup) or "—"))
        print(f"agent     {shlex.join(agent_argv(rcfg, PROMPTS[capability]))}\ntimeout   {rcfg.agent_timeout_minutes} min")
        print(f"log       {log_path}\nreport    {report}")
        return 0
    runs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    if not acquire(lock):
        print(f"factory: a {capability} run for {cfg.name} is already going ({lock}); skipping")
        return 0
    previous = signal.signal(signal.SIGTERM, _raise_exit)  # launchd/systemd stop → finally still cleans up

    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update(REPO=str(cfg.root), WORKTREE=str(worktree), FACTORY_RUN=f"{capability}-{stamp}",
               FACTORY_BRANCH=branch, FACTORY_REPORT=REPORT_REL, FACTORY_DEFAULT_BRANCH=cfg.default_branch)
    created = False
    try:
        prune(cfg, runs)
        with open(log_path, "w") as log:
            log.write(f"factory {capability} run {stamp} for {cfg.root}\n")
            code = _prepare_and_run(cfg, capability, worktree, branch, env, log, log_path)
        created = code != _NOT_CREATED
        if code == _NOT_CREATED:
            return 1
        if code == _NOT_OPTED_IN:
            return 0
        if code == _TIMED_OUT:
            print(f"factory: {capability} run timed out — log {log_path}")
        collect_report(worktree, log_path, report)
        deliver(cfg, report, worktree, env, log_path)
        print(f"factory: {capability} run finished (agent exit {code}) — log {log_path}")
        return code
    finally:
        if created and not keep:
            with open(log_path, "a") as log:
                print("factory: " + cleanup(cfg, worktree, branch, log))
        lock.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous)


def agent_output_path(log_path: Path) -> Path:
    return log_path.with_suffix(".agent.md")


def collect_report(worktree: Path, log_path: Path, report: Path) -> None:
    """The agent's digest file, else its final output (`claude -p` prints only its last message)."""
    written, said = worktree / REPORT_REL, agent_output_path(log_path)
    if written.is_file() and written.read_text().strip():
        shutil.copyfile(written, report)
    elif said.is_file() and said.read_text().strip():
        shutil.copyfile(said, report)


def ignore_report_dir(worktree: Path) -> None:
    exclude = Path(_git(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], worktree).strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = "/" + REPORT_REL.split("/")[0] + "/"
    if line not in (exclude.read_text().splitlines() if exclude.is_file() else []):
        with open(exclude, "a") as f:
            f.write(f"\n{line}\n")
    (worktree / REPORT_REL).parent.mkdir(parents=True, exist_ok=True)


def deliver(cfg: Config, report: Path, worktree: Path, env: dict, log_path: Path) -> None:
    if not report.is_file():
        print(f"factory: the run wrote no report ({report})")
        return
    print(f"factory: report {report}")
    wcfg = config_mod.load(worktree) if (worktree / CONFIG_NAME).is_file() else cfg
    if wcfg.report_with:
        with open(log_path, "a") as log:
            _sh(wcfg.report_with.replace("{file}", shlex.quote(str(report))), cfg.root, env, log, timeout=120)


def prepare_worktree(cfg: Config, worktree: Path, branch: str, env: dict, log) -> tuple[int, Config | None]:
    """Fresh worktree off the default branch (an existing one is reused), its config, then its setup.
    Returns (0, the worktree's config) or (a failure code, None)."""
    if worktree.is_dir():
        log.write(f"reusing worktree {worktree}\n")
    elif _sh(["git", "fetch", "--quiet", "origin", cfg.default_branch], cfg.root, env, log) or _sh(
        ["git", "worktree", "add", "--quiet", "-b", branch, str(worktree), f"origin/{cfg.default_branch}"], cfg.root, env, log
    ):
        return _NOT_CREATED, None
    # The run follows the default branch's config, not a possibly stale main checkout.
    if not (worktree / CONFIG_NAME).is_file():
        log.write(f"no {CONFIG_NAME} on origin/{cfg.default_branch}: repo not opted in\n")
        return _NOT_OPTED_IN, None
    try:
        wcfg = config_mod.load(worktree)
    except config_mod.ConfigError as exc:
        log.write(f"invalid config: {exc}\n")
        return _INVALID_CONFIG, None
    ignore_report_dir(worktree)
    for cmd in wcfg.worktree_setup:
        if _sh(cmd, worktree, env, log, timeout=1800):
            log.write(f"worktree setup `{cmd}` failed\n")
            return _SETUP_FAILED, None
    return 0, wcfg


def agent_invocation(wcfg: Config, prompt: str, worktree: Path, env: dict) -> tuple[list[str], dict] | None:
    """The agent's argv and environment for a session in `worktree`, or None when the agent CLI isn't on PATH."""
    run_env = dict(env)
    if wcfg.worktree_path:
        run_env["PATH"] = os.pathsep.join([str(worktree / p) for p in wcfg.worktree_path] + [env.get("PATH", "")])
    argv = agent_argv(wcfg, prompt)
    return (argv, run_env) if shutil.which(argv[0], path=run_env.get("PATH")) else None


def _prepare_and_run(cfg: Config, capability: str, worktree: Path, branch: str, env: dict, log, log_path: Path) -> int:
    """Fresh worktree off the default branch, its config and setup, then the agent. Returns the agent's exit code."""
    code, wcfg = prepare_worktree(cfg, worktree, branch, env, log)
    if code == _NOT_CREATED:
        print(f"factory: could not create the run worktree — see {log_path}")
        return code
    if code == _NOT_OPTED_IN:
        print(f"factory: {cfg.name} has no {CONFIG_NAME} on {cfg.default_branch} — not opted in; nothing to do")
        return code
    if code or wcfg is None:
        print(f"factory: the run could not start ({'invalid config' if code == _INVALID_CONFIG else 'worktree setup failed'}) — see {log_path}")
        return 1
    invocation = agent_invocation(wcfg, PROMPTS[capability], worktree, env)
    if invocation is None:
        print(f"factory: agent command {agent_argv(wcfg, '')[0]!r} not found on PATH — see {log_path}")
        return 1
    argv, run_env = invocation
    log.write(f"\n$ {shlex.join(argv)}  (output → {agent_output_path(log_path)})\n")
    log.flush()
    with open(agent_output_path(log_path), "w") as out:
        return _sh(argv, worktree, run_env, out, timeout=wcfg.agent_timeout_minutes * 60)
