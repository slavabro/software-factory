"""The agent session: a supervised process, not model calls inside workflow code (docs/designs/engine.md, section 3).

- One session per worktree: an exclusive lock, held for the whole session. A stale agent a dead worker left behind
  (the pidfile names it, with its start time) is stopped before the new one starts.
- Its own process group, killed as a group on cancel; the activity ends only once the group is gone (the workflow
  waits: WAIT_CANCELLATION_COMPLETED).
- The machine stays awake while it runs; Temporal's timeouts are server wall-clock timers.
- A heartbeat every 30 s with progress, which is also how a cancel arrives. If the machine slept anyway (lid closed
  on battery), a marker next to the run's log says so, and the run is recorded as interrupted, not failed.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engine import process
from engine.activities.repo import _repo_config, run_env
from engine.factory import config, runner
from engine.models import StepInput, StepResult

HEARTBEAT_EVERY = 30.0
LOCK_WAIT = 120.0  # a previous attempt still unwinding on this machine
SLEPT_SUFFIX = ".slept"


def slept_marker(log: str) -> Path:
    return Path(log + SLEPT_SUFFIX)


@activity.defn(name="run_agent_session")
async def run_agent_session(inp: StepInput) -> StepResult:
    worktree = Path(inp.paths.worktree)
    if not worktree.is_dir():
        raise ApplicationError(f"no worktree at {worktree}", type="WorktreeMissing")

    def resolve() -> tuple[list[str], dict[str, str]] | None:
        cfg = _repo_config(inp.ref.repo)
        wcfg = config.load(worktree)
        result: tuple[list[str], dict[str, str]] | None = runner.agent_invocation(
            wcfg, inp.prompt, worktree, run_env(cfg, inp.paths, worktree.name)
        )
        return result

    invocation = await asyncio.to_thread(resolve)
    if invocation is None:
        raise ApplicationError("the agent command is not on PATH", type="AgentMissing", non_retryable=True)
    argv, env = invocation

    lock = process.WorktreeLock(worktree)
    stale = await asyncio.to_thread(process.kill_stale, worktree)  # frees a lock a dead worker's agent held
    if not await asyncio.to_thread(lock.acquire, LOCK_WAIT):
        raise ApplicationError(f"another session holds {worktree}", type="WorktreeBusy")
    out_path = runner.agent_output_path(Path(inp.paths.log))
    try:
        with open(inp.paths.log, "a") as log:
            log.write(f"\nfactory engine: attempt {activity.info().attempt}"
                      + (f", stopped a stale agent (pid {stale})" if stale else "")
                      + f"\n$ {' '.join(argv)}  (output → {out_path})\n")
        with open(out_path, "a") as out:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=worktree, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
            )
        await asyncio.to_thread(process.write_pidfile, worktree, proc.pid)
        awake = process.keep_awake(proc.pid)
        watch = process.SleepWatch()
        started = time.time()
        try:
            while True:
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=HEARTBEAT_EVERY)
                    break
                except asyncio.TimeoutError:
                    slept = watch.tick()
                    if slept:
                        slept_marker(inp.paths.log).write_text(f"{int(slept)}\n")
                    size = out_path.stat().st_size if out_path.exists() else 0
                    activity.heartbeat({"pid": proc.pid, "output_bytes": size, "seconds": int(time.time() - started)})
        except asyncio.CancelledError:
            await asyncio.to_thread(process.kill_group, proc.pid)
            raise
        finally:
            await asyncio.to_thread(process.clear_pidfile, worktree, proc.pid)
            await asyncio.to_thread(process.stop, awake)
    finally:
        lock.release()
    if code != 0:
        raise ApplicationError(f"the agent exited with {code}; output {out_path}", type="AgentFailed", non_retryable=True)
    return StepResult(ok=True, message=f"agent finished in {int(time.time() - started) // 60} min")
