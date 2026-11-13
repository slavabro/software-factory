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
import json
import os
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
    """Next to the run's log: {"slept": seconds, "awake": seconds} of the latest attempt that noticed a sleep."""
    return Path(log + SLEPT_SUFFIX)


def _note_sleep(watch: process.SleepWatch, log: str) -> None:
    watch.tick()
    if watch.slept:
        slept_marker(log).write_text(json.dumps({"slept": int(watch.slept), "awake": int(watch.awake)}))


def _resume_watch() -> process.SleepWatch:
    """A previous attempt in this same worker process left its last clock reading in its heartbeat: compare from
    there, so a sleep that killed that attempt before it could notice is still seen."""
    details = activity.info().heartbeat_details
    last = details[0] if details else None
    if isinstance(last, dict) and last.get("worker") == os.getpid() and "wall" in last and "mono" in last:
        return process.SleepWatch(since=(float(last["wall"]), float(last["mono"])))
    return process.SleepWatch()


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
    stale = await asyncio.to_thread(process.kill_stale, worktree)  # an agent a dead worker or attempt left running
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
        watch = _resume_watch()
        started = time.time()
        try:
            while True:
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=HEARTBEAT_EVERY)
                    break
                except asyncio.TimeoutError:
                    _note_sleep(watch, inp.paths.log)
                    size = out_path.stat().st_size if out_path.exists() else 0
                    wall, mono = watch.reading()
                    activity.heartbeat({"pid": proc.pid, "output_bytes": size, "seconds": int(time.time() - started),
                                        "worker": os.getpid(), "wall": wall, "mono": mono})
        except asyncio.CancelledError:
            _note_sleep(watch, inp.paths.log)  # a cancel right after waking: still see the sleep
            await asyncio.to_thread(process.kill_group, proc.pid)
            raise
        finally:
            _note_sleep(watch, inp.paths.log)
            # Background processes the agent left behind (a dev server, a watcher) go with the session.
            await asyncio.to_thread(process.kill_group, proc.pid, 10.0)
            await asyncio.to_thread(process.clear_pidfile, worktree, proc.pid)
            await asyncio.to_thread(process.stop, awake)
    finally:
        lock.release()
    if code < 0:  # killed by a signal: a shutdown, a reboot, a stop — not the agent's own failure, worth a retry
        raise ApplicationError(f"the agent was stopped by signal {-code}; output {out_path}", type="AgentInterrupted")
    if code != 0:
        raise ApplicationError(f"the agent exited with {code}; output {out_path}", type="AgentFailed", non_retryable=True)
    return StepResult(ok=True, message=f"agent finished in {int(time.time() - started) // 60} min")
