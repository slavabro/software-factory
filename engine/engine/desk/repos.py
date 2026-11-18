"""What the desk shows about a repo beyond the engine: the factory commit it pins (local git, on its already-fetched
default branch), its default branch's CI and its open factory PRs (`gh`, cached per repo for ten minutes).

A `gh` read that fails never fails a page: the page gets the last good value with its age and the reason, or
"unknown" and the reason when there is none yet. A failed read is tried again after a minute, so a page doesn't wait
on a dead network at every load.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from engine.factory import config

GH_TIMEOUT = 15  # seconds a `gh` call may take
FRESH = 600  # a good read is kept this long
RETRY = 60  # a failed read is tried again after this long
GIT_TIMEOUT = 10
FACTORY_BRANCH = "factory/"  # the branches factory runs push (sfactory/guard.py holds runs to them)
PIN_ERE = r"software-factory(\.git)?(@|/archive/)[0-9a-f]{7,40}"  # for git grep -E
PIN = re.compile(r"software-factory(?:\.git)?(?:@|/archive/)([0-9a-f]{7,40})")
PIN_FILES = (".github", "*.txt", "*.toml", "*.lock", "*.cfg", "*.yml", "*.yaml")  # where a repo pins the factory
FAILING = {"failure", "timed_out", "startup_failure"}

T = TypeVar("T")


class GhError(Exception):
    """A `gh` read failed; the message is the reason in words."""


@dataclass(frozen=True)
class CI:
    state: str  # passing, failing, running, none (no runs on the branch)
    commit: str = ""
    url: str = ""  # the failing run, else the latest
    failing: tuple[str, ...] = ()  # names of the failing workflows


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    url: str
    branch: str
    draft: bool


@dataclass(frozen=True)
class Read(Generic[T]):
    """One cached read: the last good value and when it was read, and why the latest try failed ("" if it didn't)."""

    value: T | None = None
    read_at: float | None = None
    error: str = ""
    tried_at: float = 0.0

    def age(self, now: float | None = None) -> float | None:
        return None if self.read_at is None else (now or time.time()) - self.read_at


@dataclass(frozen=True)
class RepoGitHub:
    ci: Read[CI]
    prs: Read[tuple[PR, ...]]


def run_gh(root: Path, *args: str) -> Any:
    """`gh <args>` in the repo (gh finds the GitHub repo from its remote), parsed JSON; GhError with the reason."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    try:
        out = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=GH_TIMEOUT,
                             stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        raise GhError("gh is not installed or not on the desk's PATH") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"gh took longer than {GH_TIMEOUT} s") from None
    except OSError as e:
        raise GhError(f"gh could not run: {e}") from None
    if out.returncode:
        lines = (out.stderr or out.stdout).strip().splitlines()
        raise GhError(f"gh failed: {lines[-1][:200] if lines else f'exit {out.returncode}'}")
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise GhError("gh returned something that isn't JSON") from None


def default_branch(root: Path) -> str:
    branch: str = config.load(root).default_branch
    return branch


def ci_from_runs(runs: list[dict[str, Any]]) -> CI:
    """The latest commit's CI from `gh run list` rows (newest first): failing if any workflow failed, running if any
    has not finished, else passing. Each workflow counts once, by its newest run."""
    if not runs:
        return CI("none")
    head = str(runs[0].get("headSha", ""))
    newest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get("headSha") == head:
            newest.setdefault(str(run.get("workflowName", "")), run)
    failing = tuple(sorted(name for name, r in newest.items() if r.get("conclusion") in FAILING))
    if failing:
        url = next(str(r.get("url", "")) for name, r in newest.items() if name == failing[0])
        return CI("failing", head, url, failing)
    state = "running" if any(r.get("status") != "completed" for r in newest.values()) else "passing"
    return CI(state, head, str(runs[0].get("url", "")))


def read_ci(root: Path) -> CI:
    rows = run_gh(root, "run", "list", "--branch", default_branch(root), "--limit", "20",
                  "--json", "headSha,status,conclusion,workflowName,url")
    if not isinstance(rows, list):
        raise GhError("gh returned no list of runs")
    return ci_from_runs(rows)


def read_prs(root: Path) -> tuple[PR, ...]:
    rows = run_gh(root, "pr", "list", "--state", "open", "--limit", "100",
                  "--json", "number,title,url,headRefName,isDraft")
    if not isinstance(rows, list):
        raise GhError("gh returned no list of PRs")
    return tuple(PR(int(r["number"]), str(r.get("title", "")), str(r.get("url", "")), str(r["headRefName"]),
                    bool(r.get("isDraft"))) for r in rows if str(r.get("headRefName", "")).startswith(FACTORY_BRANCH))


def factory_pins(root: Path) -> tuple[str, ...] | None:
    """The factory commits the repo's default branch pins (its action and runtime pins should agree), read with local
    git from the already-fetched origin/<default>; () when it pins none, None when git can't tell."""
    try:
        ref = f"origin/{default_branch(root)}"
        out = subprocess.run(["git", "grep", "-I", "-h", "-o", "-E", PIN_ERE, ref, "--", *PIN_FILES], cwd=root,
                             capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:  # noqa: BLE001 — a missing checkout or a broken config: the page says unknown
        return None
    if out.returncode not in (0, 1):  # 1: no match
        return None
    return tuple(sorted({m.group(1) for m in PIN.finditer(out.stdout)}))


class GitHub:
    """The per-repo `gh` cache, shared by the desk's request threads."""

    def __init__(self, fresh: float = FRESH, retry: float = RETRY) -> None:
        self.fresh, self.retry = fresh, retry
        self._lock = threading.Lock()  # guards the two dicts
        self._reads: dict[tuple[str, str], Read[Any]] = {}
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}

    def _get(self, root: Path, kind: str, fetch: Callable[[Path], T]) -> Read[T]:
        key = (str(root), kind)
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:  # pages loading at once run `gh` once per key; the others wait for its answer
            now = time.time()
            with self._lock:
                have: Read[T] = self._reads.get(key, Read())
            if have.read_at is not None and not have.error and now - have.read_at < self.fresh:
                return have
            if have.error and now - have.tried_at < self.retry:
                return have
            try:
                got: Read[T] = Read(fetch(root), time.time(), "", now)
            except GhError as e:
                got = Read(have.value, have.read_at, str(e), now)
            except Exception as e:  # noqa: BLE001 — never fail the page over one repo's read
                got = Read(have.value, have.read_at, f"could not read: {e}"[:200], now)
            with self._lock:
                self._reads[key] = got
            return got

    def repo(self, root: Path) -> RepoGitHub:
        return RepoGitHub(self._get(root, "ci", read_ci), self._get(root, "prs", read_prs))

    def repos(self, roots: list[Path]) -> dict[str, RepoGitHub]:
        """Every repo's reads at once, so a page waits for the slowest repo, not the sum."""
        if not roots:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(roots))) as pool:
            return dict(zip((str(r) for r in roots), pool.map(self.repo, roots)))
