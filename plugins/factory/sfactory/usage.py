"""Token usage and its price, one record per assistant turn, from Claude Code's session transcripts.

F-31's foundation: the one owner of usage records and model prices. Stable interface (the costs page, F-33, and the
efficiency scout, F-32, build on it; change it only additively):

    records(since=None, until=None, projects=None, cache=None, prices=None) -> Iterator[UsageRecord]
        Every assistant turn under `projects` (default ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects), oldest
        first, deduplicated by message id. `since`/`until` are aware datetimes. `prices` is a Prices, or a function
        Where -> Prices (a repo's own overrides); default DEFAULT_PRICES.
    UsageRecord                       the fields below; `tokens` = input + output + cache_write + cache_read
    resolve_repo(cwd) -> Where        repo name, capability (factory runs), main checkout
    Prices, Price, DEFAULT_PRICES     USD per million tokens; Prices.with_overrides({model: {key: value}})
    canonical_model(model) -> str     "us.anthropic.claude-opus-5-5-20260101[1m]" -> "claude-opus-5-5"

Where the numbers come from: Claude Code writes one JSONL file per session in ~/.claude/projects/<encoded cwd>/,
and one per subagent in <session>/subagents/agent-<id>.jsonl beside an agent-<id>.meta.json (its `agentType`).
Each assistant record carries message.id, message.model, message.usage and its content blocks; a message streamed
in several blocks is written once per block with the same usage, so turns merge by message id (counts: the
largest seen; tool names: all blocks). Only metrics are kept: no prompt, reply, tool input or tool output is ever
read into a record or the cache.

Speed: a per-transcript cache (CACHE/files/<hash>.json, keyed by path, size and mtime) keeps the parsed turns and
the byte offset read so far. An unchanged file is not opened; a grown one is read from its offset. Prices apply at
read time, so a price change needs no re-scan.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .runner import HOME

# Where factory runs work (one owner each: sfactory/runner.py `run`, engine/engine/activities/repo.py `resolve`).
RUNS = HOME / "runs"  # <repo>/<capability>-<YYYYmmdd>-<HHMM>
ENGINE_WORKTREES = Path(os.environ.get("FACTORY_ENGINE_STATE", "~/.local/state/software-factory")).expanduser() / "worktrees"  # <repo>/<team>/<run>
CACHE = HOME / "usage"
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
CACHE_VERSION = 1
_RUN_DIR = re.compile(r"^(?P<cap>.+)-\d{8}-\d{4}$")


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
PRICE_KEYS = tuple(f.name for f in fields(Price) if f.name != "long")


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
                missing = [k for k in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read") if k not in keys]
                if missing:
                    raise ValueError(f"[cost.prices.{model}] is a new model: also set {', '.join(missing)}")
                base = Price(0, 0, 0, 0, 0)
            models[name] = replace(base, **{k: (int(v) if k == "long_above" else float(v)) for k, v in keys.items()})
        return Prices(models, self.web_search_usd if web_search_usd is None else float(web_search_usd))

    def cost(self, model: str, input: int, output: int, cw5: int, cw1h: int, cr: int, speed: str | None = None,
             geo: str | None = None, web_searches: int = 0) -> tuple[float, bool]:
        """(USD, priced). A model with no price costs 0 and comes back unpriced."""
        price = self.models.get(canonical_model(model))
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


# ── Records ─────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UsageRecord:
    session_id: str
    ts: dt.datetime  # aware, UTC
    cwd: str
    repo: str
    branch: str
    model: str
    input: int
    output: int
    cache_write: int  # 5-minute and 1-hour writes together
    cache_read: int
    cost_usd: float
    is_subagent: bool
    agent_type: str  # the subagent's type ("Explore", "general-purpose" …); "" for a main session
    tool_names: tuple[str, ...]
    message_id: str = ""
    cache_write_1h: int = 0
    capability: str | None = None  # a factory run's capability or engine team
    priced: bool = True
    speed: str = ""
    geo: str = ""
    web_searches: int = 0
    worktree: str = ""  # the cwd's directory name
    transcript: str = ""  # the JSONL file the turn came from
    repo_root: str | None = None  # the repo's main checkout, when known

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read


# A turn as cached: [message id, ts, session, cwd, branch, model, input, output, cw5, cw1h, cache read, speed, geo,
# web searches, tool names, sidechain]
_T = {k: i for i, k in enumerate(
    ["mid", "ts", "sid", "cwd", "branch", "model", "in", "out", "cw5", "cw1h", "cr", "speed", "geo", "ws", "tools", "side"]
)}
_COUNTS = ("in", "out", "cw5", "cw1h", "cr", "ws")


def _turn(d: dict) -> list | None:
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage") or {}
    model = m.get("model") or ""
    if not u or model.startswith("<"):  # "<synthetic>": written by the client, never billed
        return None
    cw = int(u.get("cache_creation_input_tokens") or 0)
    split = u.get("cache_creation") or {}
    cw1h = min(cw, int(split.get("ephemeral_1h_input_tokens") or 0))
    tools = sorted({c.get("name", "") for c in m.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"} - {""})
    return [
        m.get("id") or d.get("requestId") or d.get("uuid") or "", d.get("timestamp") or "", d.get("sessionId") or "",
        d.get("cwd") or "", d.get("gitBranch") or "", model, int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0), cw - cw1h, cw1h, int(u.get("cache_read_input_tokens") or 0),
        u.get("speed") or "", u.get("inference_geo") or "",
        int((u.get("server_tool_use") or {}).get("web_search_requests") or 0), tools, bool(d.get("isSidechain")),
    ]


def _merge(turns: dict[str, list], t: list) -> None:
    old = turns.get(t[0])
    if old is None:
        turns[t[0]] = t
        return
    for k in _COUNTS:
        old[_T[k]] = max(old[_T[k]], t[_T[k]])
    old[_T["tools"]] = sorted(set(old[_T["tools"]]) | set(t[_T["tools"]]))


def _read(path: Path, start: int, turns: dict[str, list]) -> int:
    """Parse complete lines from byte `start`; returns the offset after the last complete line."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        if b'"type":"assistant"' not in line and b'"type": "assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        t = _turn(d)
        if t is not None:
            _merge(turns, t)
    return start + end


def _head(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(512)).hexdigest()


def _agent_type(path: Path) -> str:
    meta = path.with_suffix(".meta.json")
    try:
        return str(json.loads(meta.read_text()).get("agentType") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def scan_file(path: Path, cache: Path) -> dict:
    """The transcript's cached entry, brought up to date: {path, size, mtime, offset, head, subagent, agent_type,
    turns}. Reads only what is new since the last scan."""
    st = path.stat()
    entry_path = cache / "files" / (hashlib.sha1(str(path).encode()).hexdigest() + ".json")
    entry = None
    try:
        entry = json.loads(entry_path.read_text())
        if entry.get("v") != CACHE_VERSION or entry.get("path") != str(path):
            entry = None
    except (OSError, ValueError):
        pass
    if entry and entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
        return entry
    turns: dict[str, list] = {}
    start = 0
    if entry and st.st_size > entry["offset"] and entry["size"] <= st.st_size and entry.get("head") == _head(path):
        turns = {t[0]: t for t in entry["turns"]}
        start = entry["offset"]
    subagent = path.parent.name == "subagents"
    entry = {
        "v": CACHE_VERSION, "path": str(path), "size": st.st_size, "mtime": st.st_mtime,
        "offset": _read(path, start, turns), "head": _head(path), "subagent": subagent,
        "agent_type": _agent_type(path) if subagent else "", "turns": list(turns.values()),
    }
    _write_json(entry_path, entry)
    return entry


def transcripts(projects: Path) -> list[Path]:
    """Every session and subagent transcript, in a stable order."""
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.glob("*/*.jsonl")) + sorted(projects.glob("*/*/subagents/*.jsonl"))


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
    """A factory run's worktree gives repo and capability from its path; anything else resolves through git (a
    worktree counts for its main checkout); a cwd that is gone resolves from its nearest existing ancestor."""
    path = Path(cwd)
    for base, depth in ((RUNS, 2), (ENGINE_WORKTREES, 2)):
        try:
            rel = path.relative_to(base).parts
        except ValueError:
            continue
        if len(rel) >= depth:
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


class _Repos:
    """resolve_repo, remembered in CACHE/repos.json: a git call per new cwd, not per run."""

    def __init__(self, cache: Path):
        self.path = cache / "repos.json"
        try:
            raw = json.loads(self.path.read_text())
            self.known = {k: Where(*v) for k, v in raw.items()} if raw.pop("_v", None) == CACHE_VERSION else {}
        except (OSError, ValueError, TypeError):
            self.known = {}
        self.dirty = False

    def __call__(self, cwd: str) -> Where:
        w = self.known.get(cwd)
        if w is None:
            w = self.known[cwd] = resolve_repo(cwd)
            self.dirty = True
        return w

    def save(self) -> None:
        if self.dirty:
            data: dict = {k: [w.repo, w.capability, w.root] for k, w in self.known.items()}
            data["_v"] = CACHE_VERSION
            _write_json(self.path, data)


# ── Records ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _ts(value: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def records(
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    projects: Path | None = None,
    cache: Path | None = None,
    prices: Prices | Callable[[Where], Prices] | None = None,
) -> Iterator[UsageRecord]:
    """Every assistant turn, oldest first, deduplicated by message id across all transcripts."""
    projects = PROJECTS if projects is None else projects
    cache = CACHE if cache is None else cache
    price_for: Callable[[Where], Prices]
    if prices is None:
        price_for = lambda _where: DEFAULT_PRICES  # noqa: E731
    elif isinstance(prices, Prices):
        price_for = lambda _where, p=prices: p  # noqa: E731
    else:
        price_for = prices
    repos = _Repos(cache)
    seen: set[str] = set()
    out: list[UsageRecord] = []
    for path in transcripts(projects):
        try:
            entry = scan_file(path, cache)
        except OSError:
            continue  # vanished or unreadable between listing and reading
        for t in entry["turns"]:
            if t[0] in seen:
                continue
            seen.add(t[0])
            ts = _ts(t[_T["ts"]])
            if ts is None or (since and ts < since) or (until and ts >= until):
                continue
            where = repos(t[_T["cwd"]])
            cw5, cw1h = t[_T["cw5"]], t[_T["cw1h"]]
            usd, priced = price_for(where).cost(
                t[_T["model"]], t[_T["in"]], t[_T["out"]], cw5, cw1h, t[_T["cr"]], t[_T["speed"]], t[_T["geo"]], t[_T["ws"]]
            )
            out.append(UsageRecord(
                session_id=t[_T["sid"]] or path.stem, ts=ts, cwd=t[_T["cwd"]], repo=where.repo, branch=t[_T["branch"]],
                model=t[_T["model"]], input=t[_T["in"]], output=t[_T["out"]], cache_write=cw5 + cw1h,
                cache_read=t[_T["cr"]], cost_usd=usd, is_subagent=entry["subagent"] or t[_T["side"]],
                agent_type=entry["agent_type"], tool_names=tuple(t[_T["tools"]]), message_id=t[0], cache_write_1h=cw1h,
                capability=where.capability, priced=priced, speed=t[_T["speed"]], geo=t[_T["geo"]],
                web_searches=t[_T["ws"]], worktree=Path(t[_T["cwd"]]).name, transcript=entry["path"], repo_root=where.root,
            ))
    repos.save()
    out.sort(key=lambda r: r.ts)
    yield from out


def matches(value: str | None, patterns: Iterable[str]) -> bool:
    """Whether `value` matches any of the globs (a missing value matches none)."""
    return value is not None and any(fnmatch.fnmatchcase(value, p) for p in patterns)
