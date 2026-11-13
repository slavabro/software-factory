"""The agent session: a supervised process, not model calls inside workflow code (docs/designs/engine.md, section 3).

- Its own process group, a pidfile and a lock in the worktree: a worker that died can't leave a second agent writing
  to the same branch (a stale group found at start is killed first).
- The machine stays awake while it runs (`caffeinate` on macOS, `systemd-inhibit` on Linux): Temporal's timeouts are
  server wall-clock timers, and a laptop that slept mid-run would wake to timeouts that already fired.
- A heartbeat every 30 s with progress; it is also how a cancel arrives. On cancel the group is killed and the
  activity ends only once it is gone (the workflow waits: WAIT_CANCELLATION_COMPLETED).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engine.activities.repo import _repo_config, run_env
from engine.factory import config, runner
from engine.models import StepInput, StepResult

HEARTBEAT_EVERY = 30.0
KILL_GRACE = 30.0
PIDFILE = Path(".factory") / "agent.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pid: int, grace: float = KILL_GRACE) -> None:
    """TERM the process group led by `pid`, then KILL what is left after `grace` seconds. A group that answers
    EPERM has only exited-but-unreaped members left (macOS reports zombies that way): it is gone."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.2)


def kill_stale(worktree: Path) -> int | None:
    """Kill an agent group a previous attempt left running in `worktree`; returns its pid, if there was one."""
    pidfile = worktree / PIDFILE
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    if _alive(pid):
        kill_group(pid)
    pidfile.unlink(missing_ok=True)
    return pid


def keep_awake(pid: int) -> subprocess.Popen[bytes] | None:
    """Hold off idle sleep while `pid` lives; None where the platform has no way."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i", "-w", str(pid)]
    elif shutil.which("systemd-inhibit"):
        cmd = ["systemd-inhibit", "--what=idle:sleep", "--who=software-factory", "--why=agent session",
               "tail", f"--pid={pid}", "-f", "/dev/null"]
    else:
        return None
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@activity.defn(name="run_agent_session")
async def run_agent_session(inp: StepInput) -> StepResult:
    worktree = Path(inp.paths.worktree)
    if not worktree.is_dir():
        raise ApplicationError(f"no worktree at {worktree}", type="WorktreeMissing")
    cfg = _repo_config(inp.ref.repo)
    wcfg = config.load(worktree)
    invocation = runner.agent_invocation(wcfg, inp.prompt, worktree, run_env(cfg, inp.paths, worktree.name))
    if invocation is None:
        raise ApplicationError(
            f"agent command {runner.agent_argv(wcfg, '')[0]!r} is not on PATH", type="AgentMissing", non_retryable=True
        )
    argv, env = invocation
    info = activity.info()
    stale = kill_stale(worktree)
    out_path = runner.agent_output_path(Path(inp.paths.log))
    with open(inp.paths.log, "a") as log:
        log.write(f"\nfactory engine: attempt {info.attempt}"
                  + (f", stopped a stale agent (pid {stale})" if stale else "")
                  + f"\n$ {' '.join(argv)}  (output → {out_path})\n")
    with open(out_path, "a") as out:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=worktree, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
        )
    pidfile = worktree / PIDFILE
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(proc.pid))
    awake = keep_awake(proc.pid)
    started = time.time()
    try:
        while True:
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=HEARTBEAT_EVERY)
                break
            except asyncio.TimeoutError:
                size = out_path.stat().st_size if out_path.exists() else 0
                activity.heartbeat({"pid": proc.pid, "output_bytes": size, "seconds": int(time.time() - started)})
    except asyncio.CancelledError:
        await asyncio.to_thread(kill_group, proc.pid)
        raise
    finally:
        pidfile.unlink(missing_ok=True)
        if awake is not None:
            awake.terminate()
    if code != 0:
        raise ApplicationError(f"the agent exited with {code}; output {out_path}", type="AgentFailed", non_retryable=True)
    return StepResult(ok=True, message=f"agent finished in {int(time.time() - started) // 60} min")
