"""Supervising the processes a run starts (the agent CLI, the repo's setup): each in its own process group, recorded
in a pidfile with its start time so a reused pid is never mistaken for it, one at a time per worktree (a lock), and
killed as a group (docs/designs/engine.md, section 3).

Everything here blocks; activities call it through `asyncio.to_thread` so the worker's event loop never stalls.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

KILL_GRACE = 30.0
STATE_DIR = Path(".factory")  # inside a worktree; git-excluded by the runner
PIDFILE = STATE_DIR / "agent.pid"
LOCKFILE = STATE_DIR / "agent.lock"


def started_at(pid: int) -> str | None:
    """The process's start time as `ps` reports it, or None when no such process is running."""
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip() or None


def group_alive(pgid: int) -> bool:
    """Any process left in the group. EPERM means only exited-but-unreaped members are left (macOS reports zombies
    that way): gone."""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_group(pgid: int, grace: float = KILL_GRACE) -> None:
    """TERM the process group, then KILL what is left after `grace` seconds."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return
            time.sleep(0.2)


def write_pidfile(worktree: Path, pid: int) -> None:
    path = worktree / PIDFILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": pid, "started": started_at(pid)}))
    tmp.replace(path)


def clear_pidfile(worktree: Path, pid: int) -> None:
    """Remove the pidfile only if it is still ours (a later attempt may have written its own)."""
    path = worktree / PIDFILE
    try:
        if json.loads(path.read_text()).get("pid") == pid:
            path.unlink()
    except (OSError, ValueError):
        pass


def kill_stale(worktree: Path, grace: float = KILL_GRACE) -> int | None:
    """Kill the agent group a previous attempt or a dead worker left in `worktree`, if the recorded process is still
    the same one (pid and start time match); returns its pid."""
    path = worktree / PIDFILE
    try:
        record = json.loads(path.read_text())
        pid = int(record["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    killed = None
    now = started_at(pid)
    same_leader = bool(record.get("started")) and now == record["started"]
    leader_gone_group_left = now is None and group_alive(pid)  # its pid can't be reused while the group exists
    if same_leader or leader_gone_group_left:
        kill_group(pid, grace)
        killed = pid
    path.unlink(missing_ok=True)
    return killed


class WorktreeLock:
    """An exclusive lock on the worktree for one session; released when the holder exits, even if it crashed."""

    def __init__(self, worktree: Path) -> None:
        self.path = worktree / LOCKFILE
        self.handle: IO[str] | None = None

    def acquire(self, timeout: float) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self.handle.close()
                    self.handle = None
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


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


def stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class SleepWatch:
    """Notices that the machine slept: wall-clock time moves on during sleep, the monotonic clock does not (macOS,
    and Linux's CLOCK_MONOTONIC). A gap of more than `threshold` seconds between the two counts as a sleep.
    `since` resumes from a previous reading (another attempt in the same worker process: same monotonic clock)."""

    def __init__(self, threshold: float = 60.0, since: tuple[float, float] | None = None) -> None:
        self.threshold = threshold
        self.wall, self.mono = since or (time.time(), time.monotonic())
        self.start_mono = time.monotonic()
        self.slept = 0.0

    @property
    def awake(self) -> float:
        return time.monotonic() - self.start_mono

    def reading(self) -> tuple[float, float]:
        return self.wall, self.mono

    def tick(self) -> float:
        """Seconds slept since the last tick (0 when none)."""
        wall, mono = time.time(), time.monotonic()
        gap = (wall - self.wall) - (mono - self.mono)
        self.wall, self.mono = wall, mono
        if gap > self.threshold:
            self.slept += gap
            return gap
        return 0.0
