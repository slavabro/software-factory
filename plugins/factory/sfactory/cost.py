"""Agent spend: who spent it (department, agent, task), totals, budgets, and the checks that hold runs to them.

    factory cost [--since 7d|30d|month|today|DATE] [--by department|agent|task|repo|session|model] [--json]
    factory cost budgets [--json]          each budget's state today and this month; exit 1 when any is over
    factory cost page [--since …] [--out PATH] [--open]
                                           the costs page: one self-contained HTML file
    factory cost check --capability C      exit 1 with the reason when a hard budget stops this repo's C runs
    factory cost hook                      SessionStart: one line per budget at its danger line or over, else silent
    factory cost refresh                   bring the usage cache and the saved budget state up to date

Spend is priced at API list rates (sfactory/usage.py); on a subscription it is the value of the work, not a bill.
Config: [cost], [departments.*] and [[budgets]] in the user file and in each repo's .factory.toml
(docs/factory-toml.md). Spec: docs/tasks/F-33-costs-budgets.md.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import config as config_mod
from . import costconfig, usage
from .costconfig import Budget, CostConfig, Department
from .usage import UsageRecord, Where

NO_DEPARTMENT = "unassigned"
NO_TASK = "no task"
INTERACTIVE = "interactive"
STATE_FILE = "state.json"
DEFAULT_STALE_HOURS = 6.0
BY = ("department", "agent", "task", "repo", "session", "model")

# Budget states, as the product's LLM-usage page: a word and a shape, never colour alone (CLAUDE.md rule 6).
OVER, NEAR, APPROACHING, WITHIN = "over", "near", "approaching", "within"
STATE_WORDS = {OVER: "Over limit", NEAR: "Near limit", APPROACHING: "Approaching", WITHIN: "Within budget"}
STATE_SHAPES = {OVER: "✕", NEAR: "!", APPROACHING: "!", WITHIN: "✓"}
SEVERITY = {WITHIN: 0, APPROACHING: 1, NEAR: 2, OVER: 3}


# ── Settings: the user file plus each repo's own ─────────────────────────────────────────────────────────────────


class Settings:
    """The user-level cost config and, loaded on first use, each repo's (from its main checkout)."""

    def __init__(self, user: CostConfig | None = None, current: config_mod.Config | None = None):
        self.user = user or CostConfig()
        self.repos: dict[str, CostConfig | None] = {}
        self.notes: list[str] = []
        self._prices: dict[str, usage.Prices] = {}
        if current is not None:
            self.repos[current.name] = current.cost

    @classmethod
    def load(cls, repo_root: Path | None = None) -> "Settings":
        user = costconfig.load_user()
        current = None
        if repo_root is not None and (repo_root / config_mod.CONFIG_NAME).is_file():
            current = config_mod.load(repo_root)
        return cls(user, current)

    def repo(self, where: Where) -> CostConfig | None:
        if where.repo not in self.repos:
            cfg = None
            if where.root and (Path(where.root) / config_mod.CONFIG_NAME).is_file():
                try:
                    cfg = config_mod.load(Path(where.root)).cost
                except (config_mod.ConfigError, OSError, ValueError) as exc:
                    self.notes.append(f"{where.repo}: its {config_mod.CONFIG_NAME} was skipped ({exc})")
            self.repos[where.repo] = cfg
        return self.repos[where.repo]

    def _setting(self, repo: str | None, key: str):
        own = self.repos.get(repo) if repo else None
        value = getattr(own.settings, key) if own else None
        return value if value is not None else getattr(self.user.settings, key)

    def prices(self, where: Where) -> usage.Prices:
        if where.repo not in self._prices:
            p = usage.DEFAULT_PRICES.with_overrides(self.user.settings.prices, self.user.settings.web_search_usd)
            own = self.repo(where)
            if own:
                p = p.with_overrides(own.settings.prices, own.settings.web_search_usd)
            self._prices[where.repo] = p
        return self._prices[where.repo]

    def task_pattern(self, repo: str) -> re.Pattern:
        return re.compile(self._setting(repo, "task_pattern") or costconfig.DEFAULT_TASK_PATTERN)

    def departments(self, repo: str) -> list[Department]:
        own = self.repos.get(repo)
        return (own.departments if own else []) + self.user.departments

    def budgets(self) -> list[Budget]:
        return self.user.budgets + [b for c in self.repos.values() if c for b in c.budgets]

    @property
    def plan_monthly_usd(self) -> float | None:
        return self.user.settings.plan_monthly_usd

    @property
    def stale_hours(self) -> float:
        value = self.user.settings.stale_hours
        return DEFAULT_STALE_HOURS if value is None else value


# ── Attribution ──────────────────────────────────────────────────────────────────────────────────────────────────


def agent_of(rec: UsageRecord) -> str:
    """`run:<capability>` for a factory run (its subagents included), `subagent:<type>`, else `interactive`."""
    if rec.capability:
        return f"run:{rec.capability}"
    if rec.is_subagent:
        return f"subagent:{rec.agent_type or 'unknown'}"
    return INTERACTIVE


def task_of(branch: str, worktree: str, pattern: re.Pattern) -> str:
    """The task ID in the branch, else in the worktree's name; letter IDs in capitals ("f-33" → "F-33")."""
    for text in (branch, worktree):
        m = pattern.search(text or "")
        if m:
            found = m.group(1) if m.groups() and m.group(1) else m.group(0)
            return found.upper() if found[:1].isalpha() else found
    return NO_TASK


def department_of(repo: str, capability: str | None, branch: str, cwd: str, agent: str,
                  departments: Iterable[Department]) -> str:
    """The first department whose every given rule matches (any glob within a rule)."""
    values = {"repos": repo, "capabilities": capability, "branches": branch, "cwds": cwd, "agents": agent}
    for d in departments:
        rules = {k: getattr(d, k) for k in values if getattr(d, k)}
        if all(usage.matches(values[k], [os.path.expanduser(p) for p in globs]) for k, globs in rules.items()):
            return d.name
    return NO_DEPARTMENT


@dataclass(frozen=True)
class Entry:
    rec: UsageRecord
    department: str
    agent: str
    task: str

    def value(self, by: str) -> str:
        if by == "repo":
            return self.rec.repo
        if by == "session":
            return self.rec.session_id
        if by == "model":
            return usage.canonical_model(self.rec.model) or self.rec.model
        return getattr(self, by)


def entries(settings: Settings, since: dt.datetime | None = None, until: dt.datetime | None = None,
            **kw) -> list[Entry]:
    """Every turn in [since, until) with its department, agent and task."""
    out = []
    for rec in usage.records(since=since, until=until, prices=settings.prices, **kw):
        agent = agent_of(rec)
        dept = department_of(rec.repo, rec.capability, rec.branch, rec.cwd, agent, settings.departments(rec.repo))
        out.append(Entry(rec, dept, agent, task_of(rec.branch, rec.worktree, settings.task_pattern(rec.repo))))
    return out


# ── Totals ───────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Totals:
    cost: float = 0.0
    turns: int = 0
    tokens: int = 0
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    unpriced: int = 0
    sessions: set = field(default_factory=set)
    first: dt.datetime | None = None
    last: dt.datetime | None = None

    def add(self, r: UsageRecord) -> None:
        self.cost += r.cost_usd
        self.turns += 1
        self.tokens += r.tokens
        self.input += r.input
        self.output += r.output
        self.cache_write += r.cache_write
        self.cache_read += r.cache_read
        self.unpriced += not r.priced
        self.sessions.add(r.session_id)
        self.first = r.ts if self.first is None or r.ts < self.first else self.first
        self.last = r.ts if self.last is None or r.ts > self.last else self.last

    @property
    def cache_hit(self) -> float | None:
        """Share of the prompt served from the cache."""
        prompt = self.input + self.cache_write + self.cache_read
        return self.cache_read / prompt if prompt else None

    def as_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("sessions", "first", "last")}
        d.update(sessions=len(self.sessions), cost=round(self.cost, 6), cache_hit=self.cache_hit,
                 first=self.first.isoformat() if self.first else None, last=self.last.isoformat() if self.last else None)
        return d


def total(items: Iterable[Entry]) -> Totals:
    t = Totals()
    for e in items:
        t.add(e.rec)
    return t


def group(items: Iterable[Entry], by: str | Callable[[Entry], str]) -> list[tuple[str, Totals]]:
    """Totals per value, most expensive first."""
    key = (lambda e: e.value(by)) if isinstance(by, str) else by
    groups: dict[str, Totals] = {}
    for e in items:
        groups.setdefault(key(e), Totals()).add(e.rec)
    return sorted(groups.items(), key=lambda kv: (-kv[1].cost, kv[0]))


# ── Periods ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Period:
    since: dt.datetime
    until: dt.datetime
    last: str  # "last 30 days"
    prior: str  # "the 30 days before"

    @property
    def prior_since(self) -> dt.datetime:
        return self.since - (self.until - self.since)

    @property
    def days(self) -> float:
        return max((self.until - self.since).total_seconds() / 86400, 1 / 24)


def local_midnight(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: dt.datetime) -> dt.datetime:
    return local_midnight(now).replace(day=1)


def period(text: str, now: dt.datetime) -> Period:
    """7d · 24h · 30d · month · today · YYYY-MM-DD (from that day)."""
    m = re.fullmatch(r"(\d+)([dh])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        span = dt.timedelta(days=n) if unit == "d" else dt.timedelta(hours=n)
        noun = f"{n} days" if unit == "d" else f"{n} hours"
        if n == 1:
            noun = noun.rstrip("s")
        return Period(now - span, now, f"last {noun}", f"the {noun} before")
    if text == "month":
        return Period(month_start(now), now, "this month", "the same span before")
    if text == "today":
        return Period(local_midnight(now), now, "today", "the same hours before")
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"factory cost: --since {text!r}: use 7d, 24h, month, today or a date (YYYY-MM-DD)") from None
    since = dt.datetime.combine(day, dt.time(), now.tzinfo)
    return Period(since, now, f"since {day.isoformat()}", "the same span before")


# ── Budgets ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Meter:
    """One limit of one budget (for a glob scope, of one matching value) over its window."""

    label: str
    window: str  # "today" | "this month"
    unit: str  # "usd" | "tokens"
    used: float
    limit: float
    pct: float  # unclamped
    state: str
    hard: bool
    warn: float
    danger: float
    scope_key: str | None
    scope_value: str | None
    owner_repo: str | None
    source: str
    each: bool = False  # one of many from a glob scope ("every task")

    @property
    def word(self) -> str:
        return STATE_WORDS[self.state]

    @property
    def shape(self) -> str:
        return STATE_SHAPES[self.state]

    def figure(self, value: float) -> str:
        return fmt_usd(value) if self.unit == "usd" else f"{fmt_tokens(value)} tokens"

    def sentence(self) -> str:
        return (f"{self.shape} {self.word}: {self.label}, {self.window} — {self.figure(self.used)} of "
                f"{self.figure(self.limit)} ({self.pct_text})")

    @property
    def pct_text(self) -> str:
        """Whole percent, never rounded up to 100 % while still under the limit."""
        return f"{math.floor(self.pct)}%" if self.pct < 100 else f"{self.pct:.0f}%"


def state_of(pct: float, warn: float, danger: float) -> str:
    if pct >= 100:
        return OVER
    if pct >= danger:
        return NEAR
    if pct >= warn:
        return APPROACHING
    return WITHIN


_GLOB = re.compile(r"[*?\[]")
_SENTINELS = {NO_DEPARTMENT, NO_TASK}


def _scope_value(e: Entry, key: str) -> str:
    return e.rec.repo if key == "repo" else getattr(e, key)


def _each_label(b: Budget, key: str, value: str) -> str:
    """One value of a glob budget: "task F-33", or "<name> · F-33" when the budget is named."""
    if b.name:
        return f"{b.name} · {value}"
    base = f"{key} {value}"
    return f"{b.owner_repo}: {base}" if b.owner_repo and key != "repo" else base


def meters(month: list[Entry], budgets: Iterable[Budget], now: dt.datetime) -> list[Meter]:
    """Every budget's limits against this month's entries (`month`) and today's, worst first."""
    today_start = local_midnight(now)
    out: list[Meter] = []
    for b in budgets:
        mine = [e for e in month if b.owner_repo is None or e.rec.repo == b.owner_repo]
        scope = b.scope
        if scope is None:
            groups = [(None, mine)]
        elif _GLOB.search(scope[1]):
            values = sorted({_scope_value(e, scope[0]) for e in mine} - _SENTINELS)
            groups = [(v, [e for e in mine if _scope_value(e, scope[0]) == v])
                      for v in values if usage.matches(v, [scope[1]])]
        else:
            groups = [(scope[1], [e for e in mine if _scope_value(e, scope[0]) == scope[1]])]
        for value, items in groups:
            label = b.label if scope is None or value == scope[1] else _each_label(b, scope[0], value)
            today = [e for e in items if e.rec.ts >= today_start]
            for window, rows, limit_usd, limit_tokens in (
                ("this month", items, b.monthly_usd, b.monthly_tokens),
                ("today", today, b.daily_usd, b.daily_tokens),
            ):
                for unit, limit in (("usd", limit_usd), ("tokens", limit_tokens)):
                    if not limit:
                        continue
                    used = sum(e.rec.cost_usd if unit == "usd" else e.rec.tokens for e in rows)
                    pct = used / limit * 100
                    out.append(Meter(label, window, unit, used, float(limit), pct, state_of(pct, b.warn, b.danger),
                                     b.hard, b.warn, b.danger, scope[0] if scope else None, value, b.owner_repo,
                                     b.source, each=bool(scope) and value != scope[1]))
    out.sort(key=lambda m: (-SEVERITY[m.state], -m.pct))
    return out


@dataclass(frozen=True)
class Probe:
    """A run about to start, as the budgets see it."""

    repo: str
    department: str
    agent: str
    task: str = NO_TASK

    def value(self, key: str) -> str:
        return getattr(self, key)


def covers(m: Meter, probe: Probe) -> bool:
    if m.owner_repo is not None and m.owner_repo != probe.repo:
        return False
    if m.scope_key is None:
        return True
    return probe.value(m.scope_key) == m.scope_value


def run_probe(settings: Settings, repo: str, capability: str) -> Probe:
    """How a `factory run <capability>` (or an engine team run) in `repo` is attributed."""
    agent = f"run:{capability}"
    cwd = str(usage.RUNS / repo / f"{capability}-00000000-0000")
    dept = department_of(repo, capability, f"factory/{capability}", cwd, agent, settings.departments(repo))
    return Probe(repo, dept, agent)


def blockers(found: Iterable[Meter], probe: Probe) -> list[Meter]:
    """Hard budgets over their limit that cover the run."""
    return [m for m in found if m.hard and m.state == OVER and covers(m, probe)]


# ── Saved state: what the SessionStart hook reads ────────────────────────────────────────────────────────────────


def save_state(found: list[Meter], cache: Path | None = None, now: dt.datetime | None = None) -> None:
    cache = usage.CACHE if cache is None else cache
    data = {"computed_at": (now or dt.datetime.now().astimezone()).isoformat(), "meters": [asdict(m) for m in found]}
    usage._write_json(cache / STATE_FILE, data)


def read_state(cache: Path | None = None) -> tuple[dt.datetime | None, list[Meter]]:
    cache = usage.CACHE if cache is None else cache
    try:
        data = json.loads((cache / STATE_FILE).read_text())
        return dt.datetime.fromisoformat(data["computed_at"]), [Meter(**m) for m in data["meters"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def evaluate(settings: Settings, now: dt.datetime | None = None, **kw) -> tuple[list[Entry], list[Meter]]:
    """This month's entries and every budget's meters; saves the state the hook reads."""
    now = now or dt.datetime.now().astimezone()
    month = entries(settings, since=month_start(now), **kw)
    found = meters(month, settings.budgets(), now)
    save_state(found, kw.get("cache"), now)
    return month, found


def run_blockers(root: Path, capability: str) -> list[str]:
    """Why a run of `capability` in the repo at `root` must not start (empty: it may). One owner for `factory run`
    and the engine's check_budget."""
    try:
        settings = Settings.load(root)
    except (config_mod.ConfigError, costconfig.CostConfigError) as exc:
        return [f"the budget config can't be read, so the run is held: {exc}"]
    if not settings.budgets():
        return []
    cfg_name = next(iter(settings.repos), None) or root.name
    probe = run_probe(settings, cfg_name, capability)
    _, found = evaluate(settings)
    return [f"{m.sentence()} — hard limit; set in {m.source}" for m in blockers(found, probe)]


# ── Formatting ───────────────────────────────────────────────────────────────────────────────────────────────────


def fmt_usd(value: float, decimals: int | None = None) -> str:
    dp = decimals if decimals is not None else (2 if abs(value) >= 1 or value == 0 else 4)
    return f"${value:,.{dp}f}"


def fmt_tokens(value: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:,.0f}"


def agent_words(agent: str) -> str:
    if agent == INTERACTIVE:
        return "Interactive session"
    kind, _, name = agent.partition(":")
    return {"run": f"Factory run: {name}", "subagent": f"Subagent: {name}"}.get(kind, agent)


# ── Commands ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _root(path: Path | None) -> Path | None:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path or Path.cwd(), capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def _report(args, settings: Settings, now: dt.datetime) -> int:
    p = period(args.since, now)
    rows = entries(settings, since=p.since, until=p.until)
    t = total(rows)
    groups = group(rows, args.by)
    if args.json:
        print(json.dumps({
            "since": p.since.isoformat(), "until": p.until.isoformat(), "by": args.by, "total": t.as_dict(),
            "rows": [{"name": name, **g.as_dict()} for name, g in groups],
        }, indent=2))
        return 0
    print(f"Spend by {args.by}, {p.last}: {fmt_usd(t.cost)} · {t.turns:,} turns · {fmt_tokens(t.tokens)} tokens"
          " (USD at API list rates)")
    if not groups:
        print("No agent turns in this period.")
        return 0
    width = min(max(len(n) for n, _ in groups), 48)
    print(f"{args.by:<{width}}  {'turns':>8}  {'tokens':>8}  {'cost':>12}  share")
    for name, g in groups:
        share = g.cost / t.cost * 100 if t.cost else 0
        bar = "█" * round(share / 5)
        print(f"{name[:width]:<{width}}  {g.turns:>8,}  {fmt_tokens(g.tokens):>8}  {fmt_usd(g.cost):>12}  {share:4.0f}% {bar}")
    unpriced = sorted({e.rec.model for e in rows if not e.rec.priced})
    if unpriced:
        print(f"! No price for {', '.join(unpriced)}: counted at $0 — add them under [cost.prices]")
    for note in settings.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _budgets(args, settings: Settings, now: dt.datetime) -> int:
    _, found = evaluate(settings, now)
    if args.json:
        print(json.dumps([{**asdict(m), "word": m.word} for m in found], indent=2))
    elif not settings.budgets():
        print(f"No budgets set. Add [[budgets]] to {costconfig.USER_CONFIG} or a repo's .factory.toml "
              "(docs/factory-toml.md).")
    elif not found:
        print("No budget has spend to measure yet.")
    else:
        for m in found:
            hard = " · hard: stops factory runs" if m.hard and m.state == OVER else ""
            print(f"{m.sentence()}{hard}")
    return 1 if any(m.state == OVER for m in found) else 0


def _check(args, settings: Settings, now: dt.datetime, root: Path | None) -> int:
    reasons = run_blockers(root or Path.cwd(), args.capability)
    for r in reasons:
        print(f"factory: {args.capability} run stopped by a budget: {r}")
    return 1 if reasons else 0


def hook(cache: Path | None = None, now: dt.datetime | None = None, spawn: bool = True) -> str:
    """The SessionStart line(s): budgets at their danger line or over, from the saved state. Starts a background
    refresh when the state is stale. Never scans, never raises."""
    try:
        now = now or dt.datetime.now().astimezone()
        computed, found = read_state(cache)
        lines = [f"factory budget: {m.sentence()}" + ("; factory runs in it are stopped" if m.hard and m.state == OVER else "")
                 for m in found if m.state in (OVER, NEAR)]
        if lines:
            lines.append("factory budget: details with `factory cost budgets`")
        try:
            stale_hours = Settings(costconfig.load_user()).stale_hours
        except (costconfig.CostConfigError, OSError):
            stale_hours = DEFAULT_STALE_HOURS
        if spawn and (computed is None or (now - computed).total_seconds() > stale_hours * 3600):
            _refresh_in_background()
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — a hook never fails a session
        return ""


def _refresh_in_background() -> None:
    if not usage.PROJECTS.is_dir():
        return
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    subprocess.Popen([sys.executable, "-m", "sfactory", "cost", "refresh"], env=env, cwd=Path.home(),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def refresh() -> int:
    """Scan what is new and save the budget state; one at a time."""
    from .runner import acquire

    usage.CACHE.mkdir(parents=True, exist_ok=True)
    lock = usage.CACHE / "refresh.lock"
    if not acquire(lock):
        return 0
    try:
        evaluate(Settings.load(None))
    finally:
        lock.unlink(missing_ok=True)
    return 0


def main(argv: list[str]) -> int:
    action = argv[0] if argv[:1] and argv[0] in ("budgets", "page", "check", "hook", "refresh") else "report"
    rest = argv[1:] if action != "report" else argv
    if action == "hook":
        out = hook()
        if out:
            print(out)
        return 0
    if action == "refresh":
        return refresh()
    ap = argparse.ArgumentParser(prog=f"factory cost{'' if action == 'report' else ' ' + action}",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, help="whose .factory.toml applies first (default: the current repo)")
    if action in ("report", "page"):
        ap.add_argument("--since", default="30d", help="7d, 30d, 24h, month, today or YYYY-MM-DD (default 30d)")
    if action == "report":
        ap.add_argument("--by", choices=BY, default="department")
    if action in ("report", "budgets"):
        ap.add_argument("--json", action="store_true")
    if action == "page":
        ap.add_argument("--out", type=Path, help=f"default {usage.CACHE / 'costs.html'}")
        ap.add_argument("--open", action="store_true", help="open it in the browser")
    if action == "check":
        ap.add_argument("--capability", required=True)
    args = ap.parse_args(rest)
    root = _root(args.repo)
    now = dt.datetime.now().astimezone()
    try:
        settings = Settings.load(root)
    except (config_mod.ConfigError, costconfig.CostConfigError) as exc:
        raise SystemExit(f"factory cost: {exc}") from None
    if action == "report":
        return _report(args, settings, now)
    if action == "budgets":
        return _budgets(args, settings, now)
    if action == "check":
        return _check(args, settings, now, root)
    from . import costpage

    started = time.monotonic()
    out = args.out or usage.CACHE / "costs.html"
    p = period(args.since, now)
    span_start = min(p.prior_since, month_start(now))
    all_rows = entries(settings, since=span_start)
    month_rows = [e for e in all_rows if e.rec.ts >= month_start(now)]
    found = meters(month_rows, settings.budgets(), now)
    save_state(found, now=now)
    html = costpage.render(costpage.Model.build(all_rows, found, settings, p, now))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"factory cost: page written to {out} ({time.monotonic() - started:.1f}s)")
    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out)], check=False)
    return 0


def days_in_month(now: dt.datetime) -> int:
    return calendar.monthrange(now.year, now.month)[1]
