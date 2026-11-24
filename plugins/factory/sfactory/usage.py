"""Agent usage and its price: one record per assistant turn, from Claude Code's session transcripts, and the same
record format for a product's runtime model calls.

F-31's foundation: the one owner of usage records and model prices (the costs page, F-33, and the efficiency scout,
F-32, build on it; change the interface only additively).

    records(since=None, until=None, *, projects=None, db=None, org=None, prices=None, task_pattern=None)
        -> list[UsageRecord]   every assistant turn, oldest first, deduplicated by message id; department, process,
                               agent and task resolved now from the raw signals stored per turn (sfactory/org.py)
    runtime_records(command, project, since, *, cwd=None, prices=None) -> list[UsageRecord]
                               a product's runtime usage: its command prints USAGE_FIELDS rows as JSONL
    UsageRecord                one format for both (`as_row()` gives USAGE_FIELDS); `tokens` = all four kinds
    resolve_repo(cwd) -> Where project, capability (factory runs), main checkout
    Prices, Price, DEFAULT_PRICES, canonical_model

Where the numbers come from: Claude Code writes one JSONL file per session in ~/.claude/projects/<encoded cwd>/,
and one per subagent in <session>/subagents/agent-<id>.jsonl beside an agent-<id>.meta.json (its `agentType`).
Each assistant record carries message.id, message.model, message.usage and its content blocks; a message streamed in
several blocks is written once per block with the same usage, so turns merge by message id. Skills show up as a
`Skill` tool call or a typed `/<command>`; both are kept as events, so the stage a turn ran in can be worked out
later. Only metrics, names and ids are kept: never a prompt, a reply, a tool input or a tool output.

Store: SQLite at ~/.cache/software-factory/usage.db (stdlib sqlite3). Per transcript it keeps size, mtime and the
byte offset read so far: an unchanged file is not opened, a grown one is read from its offset. Prices and the org
apply at read time, so changing either needs no re-scan.
"""

from __future__ import annotations

import bisect
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import org as org_mod
from .runner import HOME

# Where factory runs work (one owner each: sfactory/runner.py `run`, engine/engine/activities/repo.py `resolve`).
RUNS = HOME / "runs"  # <repo>/<capability>-<YYYYmmdd>-<HHMM>
ENGINE_WORKTREES = Path(os.environ.get("FACTORY_ENGINE_STATE", "~/.local/state/software-factory")).expanduser() / "worktrees"  # <repo>/<team>/<run>
DB = HOME / "usage.db"
CACHE = HOME / "usage"  # the page and the saved budget state
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
SCHEMA_VERSION = "3"
_RUN_DIR = re.compile(r"^(?P<cap>.+)-\d{8}-\d{4}$")
_COMMAND = re.compile(r"<command-name>/?([^<\s]+)\s*</command-name>")
RESET_COMMANDS = {"clear"}  # a typed /clear starts a new context: the stage resets to Direct
# A task ID in a branch or worktree name: "PROJ-12", "f-33" (letters, a dash, digits) or "1.60a" (dotted);
# never a run stamp ("upgrade-20261109-0600") or an agent id ("agent-a1a4…"). [cost] task_pattern overrides it.
DEFAULT_TASK_PATTERN = r"(?i)(?<![a-z0-9.])([a-z]{1,5}-\d{1,5}[a-z]?|\d{1,3}\.\d{1,3}[a-z]?)(?![0-9.])"
NO_TASK = "no task"
INTERACTIVE = "interactive"


# ── Prices ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Price:
    """USD per million tokens. `fast_multiplier` applies to every category when the turn ran in fast mode;
    `long_above` > 0 switches to `long` for a turn whose prompt (input + cache writes + cache reads) is larger."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    fast_multiplier: float = 1.0
    long_above: int = 0
    long: "Price | None" = None


def _p(i: float, o: float, w5: float, w1: float, r: float, **kw) -> Price:
    return Price(i, o, w5, w1, r, **kw)


# From Anthropic's pricing page (platform.claude.com/docs/en/about-claude/pricing), checked 2026-11-17.
# Override any of them in [cost.prices] (docs/factory-toml.md).
DEFAULT_MODEL_PRICES: dict[str, Price] = {
    "claude-fable-5-2": _p(8, 40, 10, 16, 0.20, fast_multiplier=2.0),
    "claude-mythos-5-2": _p(8, 40, 10, 16, 0.20, fast_multiplier=2.0),
    "claude-fable-5-1": _p(10, 50, 12.5, 20, 0.25),
    "claude-mythos-5-1": _p(10, 50, 12.5, 20, 0.25),
    "claude-fable-5": _p(10, 50, 12.5, 20, 1.0),
    "claude-mythos-5": _p(10, 50, 12.5, 20, 1.0),
    "claude-opus-5-5": _p(4, 20, 5, 8, 0.20, fast_multiplier=2.0),
    "claude-opus-5": _p(5, 25, 6.25, 10, 0.50, fast_multiplier=2.0),
    "claude-opus-4-8": _p(5, 25, 6.25, 10, 0.50, fast_multiplier=2.0),
    "claude-opus-4-7": _p(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-6": _p(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-5": _p(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-1": _p(15, 75, 18.75, 30, 1.50),
    "claude-opus-4": _p(15, 75, 18.75, 30, 1.50),
    "claude-sonnet-5-5": _p(2, 10, 2.5, 4, 0.10),
    "claude-sonnet-5": _p(2, 10, 2.5, 4, 0.20),
    "claude-sonnet-4-6": _p(3, 15, 3.75, 6, 0.30),
    "claude-sonnet-4-5": _p(3, 15, 3.75, 6, 0.30),
    "claude-sonnet-4": _p(3, 15, 3.75, 6, 0.30),
    "claude-haiku-5-5": _p(0.10, 0.50, 0.125, 0.20, 0.01, long_above=100_000, long=_p(0.50, 2.50, 0.625, 1.0, 0.05)),
    "claude-haiku-4-5": _p(1, 5, 1.25, 2, 0.10),
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
US_ONLY_MULTIPLIER = 1.1  # usage.inference_geo == "us"
PRICE_KEYS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "fast_multiplier")


@dataclass(frozen=True)
class Prices:
    models: dict[str, Price] = field(default_factory=lambda: dict(DEFAULT_MODEL_PRICES))
    web_search_usd: float = WEB_SEARCH_USD

    def with_overrides(self, overrides: dict[str, dict[str, float]] | None, web_search_usd: float | None = None) -> "Prices":
        """A model in `overrides` keeps every default it does not name; a new model needs all five token prices."""
        models = dict(self.models)
        for model, keys in (overrides or {}).items():
            name = canonical_model(model)
            base = models.get(name)
            if base is None:
                missing = [k for k in PRICE_KEYS[:5] if k not in keys]
                if missing:
                    raise ValueError(f"[cost.prices.{model}] is a new model: also set {', '.join(missing)}")
                base = Price(0, 0, 0, 0, 0)
            models[name] = replace(base, **{k: float(v) for k, v in keys.items()})
        return Prices(models, self.web_search_usd if web_search_usd is None else float(web_search_usd))

    def price(self, model: str) -> Price | None:
        return self.models.get(canonical_model(model))

    def cost(self, model: str, input: int, output: int, cw5: int, cw1h: int, cr: int, speed: str | None = None,
             geo: str | None = None, web_searches: int = 0) -> tuple[float, bool]:
        """(USD, priced). A model with no price costs 0 and comes back unpriced."""
        price = self.price(model)
        searches = web_searches * self.web_search_usd
        if price is None:
            return searches, False
        if price.long is not None and price.long_above and input + cw5 + cw1h + cr > price.long_above:
            price = price.long
        usd = (input * price.input + output * price.output + cw5 * price.cache_write_5m + cw1h * price.cache_write_1h
               + cr * price.cache_read) / 1_000_000
        if speed == "fast":
            usd *= price.fast_multiplier
        if geo == "us":
            usd *= US_ONLY_MULTIPLIER
        return usd + searches, True


DEFAULT_PRICES = Prices()
_DATE_SUFFIX = re.compile(r"(-\d{8}|@\d{8}|-v\d+(:\d+)?)$")


def canonical_model(model: str) -> str:
    """The model's own name without provider prefixes, date snapshots or context tags."""
    name = (model or "").strip().lower()
    name = re.sub(r"\[.*?\]$", "", name)
    name = name.rsplit("/", 1)[-1]
    if "claude-" in name:
        name = name[name.index("claude-"):]
    return _DATE_SUFFIX.sub("", name)


# ── The record ──────────────────────────────────────────────────────────────────────────────────────────────────

# The one usage format: what `as_row()` gives and what a product's runtime usage command prints, one JSON object per
# line (required: ts, model). It extends the efficiency scout's runtime schema v1 with project, department and process.
USAGE_FIELDS = {
    "ts": "ISO 8601 time of the call",
    "project": "the repo (a runtime row may leave it out: the repo whose config names the command)",
    "department": "the department that spent it (the factory's org for agent sessions, the app's own at runtime)",
    "agent": "who made the call: interactive | subagent:<type> | run:<capability>, or the app's agent or service",
    "process": "the skill, capability or app workflow it ran in ('' = none)",
    "task": "what for: a task ID, a job type",
    "run_id": "groups the calls of one session, job or run",
    "model": "the model that answered",
    "input_tokens": "int", "output_tokens": "int", "cache_write_tokens": "int", "cache_read_tokens": "int",
    "cost_usd": "float, or null in a runtime row = priced from the tokens",
}


@dataclass(frozen=True)
class UsageRecord:
    ts: dt.datetime  # aware, UTC
    project: str
    department: str
    agent: str
    process: str
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int  # 5-minute and 1-hour writes together
    cache_read_tokens: int
    cost_usd: float
    session_id: str = ""  # also the run id: a session's subagents share it
    source: str = "session"  # "session" (agent work) | "runtime" (the product's own model calls)
    # Raw signals, for sessions: what the attribution above was worked out from.
    skill: str = ""  # the stage: the last mapped skill invoked before the turn
    subagent_type: str = ""
    capability: str | None = None  # a factory run's capability or engine team
    branch: str = ""
    cwd: str = ""
    is_subagent: bool = False
    tool_names: tuple[str, ...] = ()
    message_id: str = ""
    cache_write_1h: int = 0
    priced: bool = True
    speed: str = ""
    geo: str = ""
    web_searches: int = 0
    repo_root: str | None = None  # the project's main checkout, when known

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens + self.cache_read_tokens

    @property
    def worktree(self) -> str:
        return Path(self.cwd).name if self.cwd else ""

    def as_row(self) -> dict:
        d = {k: getattr(self, k) for k in USAGE_FIELDS if k not in ("ts", "run_id")}
        d.update(ts=self.ts.isoformat(), run_id=self.session_id, cost_usd=round(self.cost_usd, 6))
        return {k: d[k] for k in USAGE_FIELDS}


# ── Where a turn ran ────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Where:
    repo: str
    capability: str | None = None
    root: str | None = None  # the main checkout


def _git_root(path: Path) -> Path | None:
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    common = Path(out.stdout.strip())
    return common.parent if common.name == ".git" else None


def _repo_name(root: Path) -> str:
    try:
        name = tomllib.loads((root / ".factory.toml").read_text()).get("repo", {}).get("name")
        return str(name) if name else root.name
    except (OSError, ValueError):
        return root.name


def resolve_repo(cwd: str) -> Where:
    """A factory run's worktree gives project and capability from its path; anything else resolves through git (a
    worktree counts for its main checkout); a cwd that is gone resolves from its nearest existing ancestor."""
    path = Path(cwd)
    for base in (RUNS, ENGINE_WORKTREES):
        try:
            rel = path.relative_to(base).parts
        except ValueError:
            continue
        if len(rel) >= 2:
            cap = rel[1]
            if base == RUNS:
                m = _RUN_DIR.match(cap)
                cap = m.group("cap") if m else cap
            root = _git_root(path) if path.is_dir() else None
            return Where(rel[0], cap, str(root) if root else None)
    probe = path
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    root = _git_root(probe) if probe.is_dir() else None
    if root:
        return Where(_repo_name(root), None, str(root))
    if probe == Path.home():
        return Where("home")
    return Where(probe.name or "/")


# ── The store ───────────────────────────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, offset INTEGER, head TEXT,
    subagent INTEGER, agent_type TEXT, agent_id TEXT);
CREATE TABLE IF NOT EXISTS turns(message_id TEXT PRIMARY KEY, file TEXT, ts TEXT, session_id TEXT, agent_id TEXT,
    cwd TEXT, branch TEXT, model TEXT, input INTEGER, output INTEGER, cw5 INTEGER, cw1h INTEGER, cr INTEGER,
    speed TEXT, geo TEXT, web_searches INTEGER, tools TEXT, sidechain INTEGER);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS turns_file ON turns(file);
CREATE TABLE IF NOT EXISTS events(file TEXT, session_id TEXT, agent_id TEXT, ts TEXT, kind TEXT, name TEXT,
    PRIMARY KEY(file, ts, kind, name));
CREATE TABLE IF NOT EXISTS repos(cwd TEXT PRIMARY KEY, repo TEXT, capability TEXT, root TEXT);
CREATE TABLE IF NOT EXISTS prs(root TEXT, branch TEXT, number INTEGER, merged_at TEXT, PRIMARY KEY(root, branch));
CREATE TABLE IF NOT EXISTS pr_fetch(root TEXT PRIMARY KEY, fetched_at TEXT, ok INTEGER);
"""


def connect(db: Path | None = None) -> sqlite3.Connection:
    """The usage store, created on first use; an older schema is dropped and rebuilt from the transcripts."""
    db = DB if db is None else db
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    row = None
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError:
        pass
    if row is None or row[0] != SCHEMA_VERSION:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
        con.commit()
    return con


def transcripts(projects: Path) -> list[Path]:
    """Every session and subagent transcript, in a stable order."""
    if not projects.is_dir():
        return []
    return sorted(projects.glob("*/*.jsonl")) + sorted(projects.glob("*/*/subagents/*.jsonl"))


def _agent_type(path: Path) -> str:
    try:
        return str(json.loads(path.with_suffix(".meta.json").read_text()).get("agentType") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _turn(d: dict, file: str, agent_id: str) -> tuple | None:
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage") or {}
    model = m.get("model") or ""
    if not u or model.startswith("<"):  # "<synthetic>": written by the client, never billed
        return None
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cw1h = min(cw, int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0))
    tools = sorted({c.get("name", "") for c in m.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "tool_use"} - {""})
    return (m.get("id") or d.get("requestId") or d.get("uuid") or "", file, d.get("timestamp") or "",
            d.get("sessionId") or "", agent_id or d.get("agentId") or "", d.get("cwd") or "", d.get("gitBranch") or "",
            model, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), cw - cw1h, cw1h,
            int(u.get("cache_read_input_tokens") or 0), u.get("speed") or "", u.get("inference_geo") or "",
            int((u.get("server_tool_use") or {}).get("web_search_requests") or 0), ",".join(tools),
            int(bool(d.get("isSidechain"))))


def _events(d: dict, file: str, agent_id: str) -> list[tuple]:
    """Skill invocations (a `Skill` tool call, a typed /command) and /clear, as (file, session, agent, ts, kind, name)."""
    out = []
    ts, sid, aid = d.get("timestamp") or "", d.get("sessionId") or "", agent_id or d.get("agentId") or ""
    content = (d.get("message") or {}).get("content")
    if d.get("type") == "assistant" and isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
                name = str((c.get("input") or {}).get("skill") or "").lstrip("/")
                if name:
                    out.append((file, sid, aid, ts, "skill", name))
    elif d.get("type") == "user":
        texts = [content] if isinstance(content, str) else [
            c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        for name in _COMMAND.findall(" ".join(t for t in texts if isinstance(t, str))):
            out.append((file, sid, aid, ts, "clear" if name in RESET_COMMANDS else "skill", name))
    return out


_UPSERT = """INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(message_id) DO UPDATE SET input=max(input, excluded.input), output=max(output, excluded.output),
  cw5=max(cw5, excluded.cw5), cw1h=max(cw1h, excluded.cw1h), cr=max(cr, excluded.cr),
  web_searches=max(web_searches, excluded.web_searches),
  tools=CASE WHEN excluded.tools = '' THEN tools WHEN tools = '' THEN excluded.tools ELSE tools || ',' || excluded.tools END
WHERE turns.file = excluded.file"""


def scan_file(con: sqlite3.Connection, path: Path) -> bool:
    """Bring one transcript's turns and events up to date; True when anything was read."""
    st = path.stat()
    key = str(path)
    row = con.execute("SELECT size, mtime, offset, head FROM files WHERE path=?", (key,)).fetchone()
    if row and row[0] == st.st_size and row[1] == st.st_mtime:
        return False
    with open(path, "rb") as f:
        first = f.read(512)
        # A fingerprint of the file's start (its length and hash, never the bytes: they hold transcript text), so a
        # file rewritten in place is read again from the beginning.
        head = f"{len(first)}:{hashlib.sha256(first).hexdigest()}"
        start = 0
        if row and st.st_size > row[2] and row[0] <= st.st_size and row[3]:
            n = int(str(row[3]).split(":", 1)[0])
            if f"{n}:{hashlib.sha256(first[:n]).hexdigest()}" == row[3]:
                start = row[2]
        else:
            con.execute("DELETE FROM turns WHERE file=?", (key,))
            con.execute("DELETE FROM events WHERE file=?", (key,))
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n") + 1
    subagent = path.parent.name == "subagents"
    agent_id = path.stem.removeprefix("agent-") if subagent else ""
    turns: dict[str, tuple] = {}
    events: list[tuple] = []
    for line in data[:end].splitlines():
        is_turn = b'"type":"assistant"' in line or b'"type": "assistant"' in line
        if not is_turn and b"command-name" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        events += _events(d, key, agent_id)
        if d.get("type") == "assistant":
            t = _turn(d, key, agent_id)
            if t is not None:
                old = turns.get(t[0])
                if old is not None:  # one message streamed as several blocks
                    t = (*t[:8], *(max(a, b) for a, b in zip(old[8:13], t[8:13])), *t[13:15], max(old[15], t[15]),
                         ",".join(sorted(set(filter(None, (old[16] + "," + t[16]).split(","))))), t[17])
                turns[t[0]] = t
    con.executemany(_UPSERT, list(turns.values()))
    con.executemany("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)", events)
    con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)",
                (key, st.st_size, st.st_mtime, start + end, head, int(subagent),
                 _agent_type(path) if subagent else "", agent_id))
    return True


def scan(projects: Path | None = None, db: Path | None = None, con: sqlite3.Connection | None = None) -> int:
    """Read what is new in every transcript; returns how many files were read."""
    projects = PROJECTS if projects is None else projects
    own = con is None
    con = connect(db) if con is None else con
    read = 0
    try:
        for path in transcripts(projects):
            try:
                read += scan_file(con, path)
            except OSError:
                continue  # vanished or unreadable between listing and reading
            if read and read % 50 == 0:
                con.commit()
        con.commit()
    finally:
        if own:
            con.close()
    return read


def _where(con: sqlite3.Connection, cwds: Iterable[str]) -> dict[str, Where]:
    known = {r[0]: Where(r[1], r[2], r[3]) for r in con.execute("SELECT cwd, repo, capability, root FROM repos")}
    new = [(c, resolve_repo(c)) for c in set(cwds) - set(known)]
    con.executemany("INSERT OR REPLACE INTO repos VALUES(?,?,?,?)", [(c, w.repo, w.capability, w.root) for c, w in new])
    known.update(new)
    return known


def known_projects(db: Path | None = None) -> dict[str, str | None]:
    """Every project the store has turns for, with its main checkout when known (so its config can be read)."""
    con = connect(db)
    try:
        where = _where(con, (r[0] for r in con.execute("SELECT DISTINCT cwd FROM turns")))
        con.commit()
    finally:
        con.close()
    out: dict[str, str | None] = {}
    for w in where.values():
        out[w.repo] = out.get(w.repo) or w.root
    return out


# ── Records ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _ts(value: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class _Stages:
    """The stage of a turn: the last skill the org maps, invoked in its session before it (/clear resets it); a
    subagent with no stage of its own inherits its parent's when it was spawned."""

    def __init__(self, con: sqlite3.Connection, org: org_mod.Org):
        self.org = org
        self.streams: dict[tuple[str, str], tuple[list[str], list[tuple[str, str]]]] = {}
        grouped: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
        for sid, aid, ts, kind, name in con.execute("SELECT session_id, agent_id, ts, kind, name FROM events ORDER BY ts"):
            grouped[(sid, aid)].append((ts, kind, name))
        for k, rows in grouped.items():
            self.streams[k] = ([r[0] for r in rows], [(r[1], r[2]) for r in rows])
        self.spawned = {(sid, aid): ts for sid, aid, ts in con.execute(
            "SELECT session_id, agent_id, min(ts) FROM turns WHERE agent_id != '' GROUP BY session_id, agent_id")}

    def _walk(self, key: tuple[str, str], ts: str, project: str) -> tuple[bool, str]:
        """(decided, skill): decided is False when the stream holds nothing that sets or resets the stage."""
        stream = self.streams.get(key)
        if not stream:
            return False, ""
        i = bisect.bisect_left(stream[0], ts) - 1
        while i >= 0:
            kind, name = stream[1][i]
            if kind == "clear":
                return True, ""
            if self.org.skill(name, project):
                return True, name
            i -= 1
        return False, ""

    def at(self, sid: str, aid: str, ts: str, project: str) -> str:
        decided, skill = self._walk((sid, aid), ts, project)
        if decided or not aid:
            return skill
        spawned = self.spawned.get((sid, aid), ts)
        return self._walk((sid, ""), spawned, project)[1]


def task_of(branch: str, worktree: str, pattern: re.Pattern) -> str:
    """The task ID in the branch, else in the worktree's name; letter IDs in capitals ("f-33" → "F-33")."""
    for text in (branch, worktree):
        m = pattern.search(text or "")
        if m:
            found = m.group(1) if m.groups() and m.group(1) else m.group(0)
            return found.upper() if found[:1].isalpha() else found
    return NO_TASK


def records(
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    *,
    projects: Path | None = None,
    db: Path | None = None,
    org: org_mod.Org | None = None,
    prices: Prices | Callable[[Where], Prices] | None = None,
    task_pattern: re.Pattern | Callable[[str], re.Pattern] | None = None,
    rescan: bool = True,
) -> list[UsageRecord]:
    """Every assistant turn in [since, until), oldest first, attributed through `org` (default: the factory's)."""
    org = org_mod.load() if org is None else org
    price_for: Callable[[Where], Prices] = (
        (lambda _w: DEFAULT_PRICES) if prices is None else (lambda _w, p=prices: p) if isinstance(prices, Prices)
        else prices)
    default_pattern = re.compile(DEFAULT_TASK_PATTERN)
    pattern_for: Callable[[str], re.Pattern] = (
        (lambda _p: default_pattern) if task_pattern is None
        else (lambda _p, t=task_pattern: t) if isinstance(task_pattern, re.Pattern) else task_pattern)
    con = connect(db)
    try:
        if rescan:
            scan(projects, con=con)
        sql = ("SELECT t.message_id, t.ts, t.session_id, t.agent_id, t.cwd, t.branch, t.model, t.input, t.output, t.cw5,"
               " t.cw1h, t.cr, t.speed, t.geo, t.web_searches, t.tools, t.sidechain, f.subagent, f.agent_type"
               " FROM turns t JOIN files f ON f.path = t.file WHERE 1=1")
        # A coarse filter on the stored text (whole seconds, so a second either side); the exact one is below.
        args: list = []
        if since:
            sql += " AND t.ts >= ?"
            args.append(_iso(since - dt.timedelta(seconds=1)))
        if until:
            sql += " AND t.ts < ?"
            args.append(_iso(until + dt.timedelta(seconds=1)))
        rows = con.execute(sql + " ORDER BY t.ts", args).fetchall()
        where = _where(con, (r[4] for r in rows))
        stages = _Stages(con, org)
        con.commit()
    finally:
        con.close()
    out = []
    for (mid, ts_text, sid, aid, cwd, branch, model, inp, outp, cw5, cw1h, cr, speed, geo, ws, tools, side,
         subagent, agent_type) in rows:
        ts = _ts(ts_text)
        if ts is None or (since and ts < since) or (until and ts >= until):
            continue
        w = where[cwd]
        is_sub = bool(subagent or side)
        if w.capability:
            agent = f"run:{w.capability}"
        elif is_sub:
            agent = f"subagent:{agent_type or 'unknown'}"
        else:
            agent = INTERACTIVE
        skill = stages.at(sid, aid, ts_text, w.repo)
        dept, process = None, ""
        if w.capability and (dept := org.capability(w.capability, w.repo)):
            process = w.capability
        elif is_sub and agent_type and (dept := org.agent(agent_type, w.repo)):
            process = agent_type
        elif skill:
            dept, process = org.skill(skill, w.repo), skill
        usd, priced = price_for(w).cost(model, inp, outp, cw5, cw1h, cr, speed, geo, ws)
        out.append(UsageRecord(
            ts=ts, project=w.repo, department=dept or org.direct, agent=agent, process=process,
            task=task_of(branch, Path(cwd).name, pattern_for(w.repo)), model=model, input_tokens=inp,
            output_tokens=outp, cache_write_tokens=cw5 + cw1h, cache_read_tokens=cr, cost_usd=usd, session_id=sid,
            skill=skill, subagent_type=agent_type if is_sub else "", capability=w.capability, branch=branch, cwd=cwd,
            is_subagent=is_sub, tool_names=tuple(sorted(set(filter(None, tools.split(","))))), message_id=mid,
            cache_write_1h=cw1h, priced=priced, speed=speed, geo=geo, web_searches=ws, repo_root=w.root,
        ))
    return out


# ── Runtime usage: the product's own model calls ────────────────────────────────────────────────────────────────


class RuntimeUsageError(RuntimeError):
    pass


def runtime_records(command: str, project: str, since: dt.datetime, *, cwd: Path | None = None,
                    prices: Prices | None = None, timeout: float = 120) -> list[UsageRecord]:
    """Run the product's usage command (`{since}` = the window start, ISO 8601, shell-quoted) and read its JSONL
    rows (USAGE_FIELDS). A row without `cost_usd` is priced from its tokens. A failure is reported by exit code
    only: its output may hold private data."""
    prices = prices or DEFAULT_PRICES
    cmd = command.replace("{since}", shlex.quote(since.isoformat()))
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeUsageError(f"{project}: the runtime usage command timed out after {timeout:.0f}s") from None
    if proc.returncode != 0:
        raise RuntimeUsageError(f"{project}: the runtime usage command exited {proc.returncode}")
    out = []
    for i, line in enumerate(proc.stdout.splitlines()):
        try:
            row = json.loads(line)
            ts = _ts(str(row["ts"]))
            model = str(row["model"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts is None or ts < since:
            continue
        n = {k: int(row.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")}
        cost = row.get("cost_usd")
        priced = True
        if cost is None:
            cost, priced = prices.cost(model, n["input_tokens"], n["output_tokens"], n["cache_write_tokens"], 0,
                                       n["cache_read_tokens"])
        out.append(UsageRecord(
            ts=ts, project=str(row.get("project") or project), department=str(row.get("department") or ""),
            agent=str(row.get("agent") or ""), process=str(row.get("process") or ""), task=str(row.get("task") or ""),
            model=model, input_tokens=n["input_tokens"], output_tokens=n["output_tokens"],
            cache_write_tokens=n["cache_write_tokens"], cache_read_tokens=n["cache_read_tokens"], cost_usd=float(cost),
            session_id=str(row.get("run_id") or ""), source="runtime", message_id=f"runtime-{i}", priced=priced,
        ))
    return out


def matches(value: str | None, patterns: Iterable[str]) -> bool:
    """Whether `value` matches any of the globs (a missing value matches none)."""
    return value is not None and any(fnmatch.fnmatchcase(value, p) for p in patterns)


def encode_project_dir(path: Path | str) -> str:
    """Claude Code's directory name for a cwd under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def spend_under(worktree: Path, projects: Path | None = None, prices: Prices | None = None) -> float:
    """What the sessions that ran in `worktree` (and below it) have cost so far: a running factory run's spend.
    Reads only that worktree's transcripts, so it is cheap to call every minute."""
    projects = PROJECTS if projects is None else projects
    prices = prices or DEFAULT_PRICES
    prefix = encode_project_dir(worktree)
    total = 0.0
    seen: set[str] = set()
    for d in projects.glob(f"{prefix}*"):
        for path in sorted(d.glob("*.jsonl")) + sorted(d.glob("*/subagents/*.jsonl")):
            turns: dict[str, tuple] = {}
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for line in data.splitlines():
                if b'"type":"assistant"' not in line:
                    continue
                try:
                    t = _turn(json.loads(line), "", "")
                except ValueError:
                    continue
                if t and t[0] not in seen:
                    turns[t[0]] = t
            for t in turns.values():
                seen.add(t[0])
                total += prices.cost(t[7], t[8], t[9], t[10], t[11], t[12], t[13], t[14], t[15])[0]
    return total
