"""Agent spend by department (a function: Product, Design, Engineering, Quality, Release, Upkeep, Chief of Staff,
Direct) and project (a repo, the second dimension), with budgets and the checks that hold runs to them.

    factory cost [--since 7d|30d|month|today|DATE] [--by department|project|agent|process|task|session|model] [--json]
    factory cost budgets [--json]          each budget's state; exit 1 when any is over
    factory cost page [--since …] [--out PATH] [--open] [--no-landed]
                                           the costs page: one self-contained HTML file
    factory cost org [--json]              the org as resolved: departments and what each owns
    factory cost check --capability C      exit 1 with the reason when a hard budget stops this repo's C runs
    factory cost hook                      SessionStart: one line per budget at its danger line or over, else silent
    factory cost refresh                   bring the usage store and the saved budget state up to date

Spend is priced at API list rates (sfactory/usage.py); on a subscription it is the value of the work, not a bill.
The org: sfactory/org.py. Config: [cost], [org], [[budgets]] in the user file and each repo's .factory.toml
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
from . import org as org_mod
from .costconfig import Budget, CostConfig
from .usage import NO_TASK, UsageRecord, Where

STATE_FILE = "state.json"
DEFAULT_STALE_HOURS = 6.0
LANDED_REFRESH_HOURS = 6.0
BY = ("department", "project", "agent", "process", "task", "session", "model")

# Budget states, as a product's LLM-usage page: a word and a shape, never colour alone (CLAUDE.md rule 6).
OVER, NEAR, APPROACHING, WITHIN = "over", "near", "approaching", "within"
STATE_WORDS = {OVER: "Over limit", NEAR: "Near limit", APPROACHING: "Approaching", WITHIN: "Within budget"}
STATE_SHAPES = {OVER: "✕", NEAR: "!", APPROACHING: "!", WITHIN: "✓"}
SEVERITY = {WITHIN: 0, APPROACHING: 1, NEAR: 2, OVER: 3}


# ── Settings: the user file, each project's own, and the org built from them ─────────────────────────────────────


class Settings:
    def __init__(self, user: CostConfig | None = None, projects: dict[str, CostConfig] | None = None,
                 org: org_mod.Org | None = None):
        self.user = user or CostConfig()
        self.projects: dict[str, CostConfig] = dict(projects or {})
        self.roots: dict[str, str] = {}
        self.notes: list[str] = []
        self._prices: dict[str, usage.Prices] = {}
        self.org = org if org is not None else self._build_org()

    def _build_org(self) -> org_mod.Org:
        org = org_mod.load(self.user.org or None, str(costconfig.USER_CONFIG))
        for project, cfg in self.projects.items():
            try:
                org_mod.add_project(org, project, cfg.org, f"{project}: {config_mod.CONFIG_NAME}")
            except org_mod.OrgError as exc:
                self.notes.append(f"{project}: its [org] was skipped ({exc})")
                org.project_maps.pop(project, None)
        return org

    @classmethod
    def load(cls, roots: dict[str, str | None] | None = None, current: Path | None = None) -> "Settings":
        """The user file, then every project's .factory.toml found at its main checkout (`roots`: project → path);
        `current` is a repo whose config must load (its errors are raised, not noted)."""
        user = costconfig.load_user()
        projects: dict[str, CostConfig] = {}
        notes, found = [], {}
        if current is not None and (current / config_mod.CONFIG_NAME).is_file():
            cfg = config_mod.load(current)
            projects[cfg.name] = cfg.cost
            found[cfg.name] = str(current)
        for project, root in (roots or {}).items():
            if project in projects or not root or not (Path(root) / config_mod.CONFIG_NAME).is_file():
                continue
            try:
                projects[project] = config_mod.load(Path(root)).cost
                found[project] = root
            except (config_mod.ConfigError, OSError, ValueError) as exc:
                notes.append(f"{project}: its {config_mod.CONFIG_NAME} was skipped ({exc})")
        s = cls(user, projects)
        s.roots = {**{p: r for p, r in (roots or {}).items() if r}, **found}
        s.notes = notes + s.notes
        return s

    def _setting(self, project: str | None, key: str):
        own = self.projects.get(project) if project else None
        value = getattr(own.settings, key) if own else None
        return value if value is not None else getattr(self.user.settings, key)

    def prices(self, where: Where | str) -> usage.Prices:
        project = where if isinstance(where, str) else where.repo
        if project not in self._prices:
            p = usage.DEFAULT_PRICES.with_overrides(self.user.settings.prices, self.user.settings.web_search_usd)
            own = self.projects.get(project)
            if own:
                p = p.with_overrides(own.settings.prices, own.settings.web_search_usd)
            self._prices[project] = p
        return self._prices[project]

    def task_pattern(self, project: str) -> re.Pattern:
        return re.compile(self._setting(project, "task_pattern") or usage.DEFAULT_TASK_PATTERN)

    def budgets(self) -> list[Budget]:
        return self.user.budgets + [b for c in self.projects.values() for b in c.budgets]

    @property
    def plan_monthly_usd(self) -> float | None:
        return self.user.settings.plan_monthly_usd

    @property
    def stale_hours(self) -> float:
        value = self.user.settings.stale_hours
        return DEFAULT_STALE_HOURS if value is None else value


def load(current: Path | None = None) -> Settings:
    """Scan what is new, then the settings for every project the store knows."""
    usage.scan()
    return Settings.load(usage.known_projects(), current)


def session_records(settings: Settings, since: dt.datetime | None = None, until: dt.datetime | None = None,
                    **kw) -> list[UsageRecord]:
    return usage.records(since, until, org=settings.org, prices=settings.prices, task_pattern=settings.task_pattern,
                         rescan=False, **kw)


def runtime_records(settings: Settings, since: dt.datetime) -> list[UsageRecord]:
    """Every project's runtime usage that names a command ([efficiency] runtime_usage)."""
    out: list[UsageRecord] = []
    for project, cfg in sorted(settings.projects.items()):
        if not cfg.runtime_usage:
            continue
        root = settings.roots.get(project)
        try:
            out += usage.runtime_records(cfg.runtime_usage, project, since, cwd=Path(root) if root else None,
                                         prices=settings.prices(project))
        except usage.RuntimeUsageError as exc:
            settings.notes.append(str(exc))
    return out


# ── Totals ───────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Totals:
    cost: float = 0.0
    calls: int = 0  # turns, for agent sessions; model calls at runtime
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
        self.calls += 1
        self.tokens += r.tokens
        self.input += r.input_tokens
        self.output += r.output_tokens
        self.cache_write += r.cache_write_tokens
        self.cache_read += r.cache_read_tokens
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


def value_of(r: UsageRecord, by: str) -> str:
    if by == "session":
        return r.session_id
    if by == "model":
        return usage.canonical_model(r.model) or r.model
    if by == "capability":
        return r.capability or ""
    return getattr(r, by)


def total(items: Iterable[UsageRecord]) -> Totals:
    t = Totals()
    for r in items:
        t.add(r)
    return t


def group(items: Iterable[UsageRecord], by: str | Callable[[UsageRecord], str]) -> list[tuple[str, Totals]]:
    """Totals per value, most expensive first."""
    key = (lambda r: value_of(r, by)) if isinstance(by, str) else by
    groups: dict[str, Totals] = {}
    for r in items:
        groups.setdefault(key(r), Totals()).add(r)
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


def days_in_month(now: dt.datetime) -> int:
    return calendar.monthrange(now.year, now.month)[1]


def period(text: str, now: dt.datetime) -> Period:
    """7d · 24h · 30d · month · today · YYYY-MM-DD (from that day)."""
    m = re.fullmatch(r"(\d+)([dh])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        span = dt.timedelta(days=n) if unit == "d" else dt.timedelta(hours=n)
        noun = f"{n} {'day' if unit == 'd' else 'hour'}{'s' if n != 1 else ''}"
        return Period(now - span, now, f"last {noun}", f"the {noun} before")
    if text == "month":
        return Period(month_start(now), now, "this month", "the same span before")
    if text == "today":
        return Period(local_midnight(now), now, "today", "the same hours before")
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"factory cost: --since {text!r}: use 7d, 24h, month, today or a date (YYYY-MM-DD)") from None
    return Period(dt.datetime.combine(day, dt.time(), now.tzinfo), now, f"since {day.isoformat()}", "the same span before")


# ── Budgets ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Meter:
    """One limit of one budget (for a glob scope, of one matching value) over its window."""

    label: str
    window: str  # "today" | "this month" | "latest run"
    unit: str  # "usd" | "tokens" | "calls"
    used: float
    limit: float
    pct: float  # unclamped
    state: str
    hard: bool
    warn: float
    danger: float
    scope: dict  # key -> value, the concrete scope this meter measures
    runtime: bool
    source: str
    each: bool = False  # one of many from a glob scope ("every task")

    @property
    def word(self) -> str:
        return STATE_WORDS[self.state]

    @property
    def shape(self) -> str:
        return STATE_SHAPES[self.state]

    @property
    def pct_text(self) -> str:
        """Whole percent, never rounded up to 100 % while still under the limit."""
        return f"{math.floor(self.pct)}%" if self.pct < 100 else f"{self.pct:.0f}%"

    def figure(self, value: float) -> str:
        if self.unit == "usd":
            return fmt_usd(value)
        return f"{fmt_tokens(value)} tokens" if self.unit == "tokens" else f"{value:,.0f} calls"

    def sentence(self) -> str:
        return (f"{self.shape} {self.word}: {self.label}, {self.window} — {self.figure(self.used)} of "
                f"{self.figure(self.limit)} ({self.pct_text})")


def state_of(pct: float, warn: float, danger: float) -> str:
    if pct >= 100:
        return OVER
    if pct >= danger:
        return NEAR
    if pct >= warn:
        return APPROACHING
    return WITHIN


_GLOB = re.compile(r"[*?\[]")
_SENTINELS = {NO_TASK, ""}


def _label(b: Budget, scope: dict, org: org_mod.Org | None) -> str:
    def show(k: str, v: str) -> str:
        return f"{org.name_of(v)}" if k == "department" and org else f"{k} {v}"

    if b.name:
        each = [v for k, v in scope.items() if _GLOB.search(getattr(b, k) or "")]
        return f"{b.name} · {' · '.join(each)}" if each else b.name
    parts = [show(k, v) for k, v in scope.items()]
    base = " · ".join(parts) if parts else "all spend"
    return f"{base} (runtime)" if b.runtime else base


def meters(month: list[UsageRecord], budgets: Iterable[Budget], now: dt.datetime,
           org: org_mod.Org | None = None) -> list[Meter]:
    """Every budget's limits against this month's records (sessions and runtime) and today's, worst first."""
    today_start = local_midnight(now)
    out: list[Meter] = []
    for b in budgets:
        pool = [r for r in month if (r.source == "runtime") == b.runtime]
        fixed = {k: v for k, v in b.scope if not _GLOB.search(v)}
        globs = {k: v for k, v in b.scope if _GLOB.search(v)}
        pool = [r for r in pool if all(value_of(r, k) == v for k, v in fixed.items())]
        groups: dict[tuple, list[UsageRecord]] = {}
        if globs:
            for r in pool:
                vals = tuple(value_of(r, k) for k in globs)
                if all(v not in _SENTINELS and usage.matches(v, [globs[k]]) for k, v in zip(globs, vals)):
                    groups.setdefault(vals, []).append(r)
        else:
            groups[()] = pool
        for vals, items in sorted(groups.items()):
            scope = {**fixed, **dict(zip(globs, vals))}
            label = _label(b, scope, org)
            today = [r for r in items if r.ts >= today_start]
            limits = [
                ("this month", items, "usd", b.monthly_spend_usd), ("today", today, "usd", b.daily_spend_usd),
                ("this month", items, "calls", b.monthly_call_count), ("this month", items, "tokens", b.monthly_tokens),
                ("today", today, "tokens", b.daily_tokens),
            ]
            if b.run_spend_usd:
                runs = group(items, "session")
                latest = max(runs, key=lambda kv: kv[1].last or now, default=None)
                limits.append(("latest run", [r for r in items if latest and r.session_id == latest[0]], "usd",
                               b.run_spend_usd))
            for window, rows, unit, limit in limits:
                if not limit:
                    continue
                used = float(len(rows)) if unit == "calls" else sum(
                    r.cost_usd if unit == "usd" else r.tokens for r in rows)
                pct = used / limit * 100
                out.append(Meter(label, window, unit, used, float(limit), pct, state_of(pct, b.warn, b.danger),
                                 b.hard, b.warn, b.danger, scope, b.runtime, b.source, each=bool(globs)))
    out.sort(key=lambda m: (-SEVERITY[m.state], -m.pct))
    return out


@dataclass(frozen=True)
class Probe:
    """A factory run about to start, as the budgets see it."""

    project: str
    department: str
    agent: str
    capability: str
    task: str = NO_TASK


def covers(m: Meter, probe: Probe) -> bool:
    if m.runtime or m.window == "latest run":  # a run's own cap is enforced while it runs
        return False
    return all(getattr(probe, k) == v for k, v in m.scope.items())


def run_probe(settings: Settings, project: str, capability: str) -> Probe:
    """How a `factory run <capability>` (or an engine team run) in `project` is attributed."""
    dept = settings.org.capability(capability, project) or settings.org.skill(f"factory:{capability}", project)
    return Probe(project, dept or settings.org.direct, f"run:{capability}", capability)


def blockers(found: Iterable[Meter], probe: Probe) -> list[Meter]:
    """Hard budgets over their limit that cover the run."""
    return [m for m in found if m.hard and m.state == OVER and covers(m, probe)]


def run_cap(settings: Settings, project: str, capability: str) -> tuple[float, str] | None:
    """The lowest hard per-run cap ($) on this repo's runs of `capability`, with where it is set."""
    caps = [(b.run_spend_usd, b.source) for b in settings.budgets()
            if b.hard and b.run_spend_usd and usage.matches(capability, [b.capability or ""])
            and (b.project is None or b.project == project)]
    return min(caps) if caps else None


# ── Saved state: what the SessionStart hook reads ────────────────────────────────────────────────────────────────


def save_state(found: list[Meter], now: dt.datetime | None = None) -> None:
    data = {"computed_at": (now or dt.datetime.now().astimezone()).isoformat(), "meters": [asdict(m) for m in found]}
    usage.CACHE.mkdir(parents=True, exist_ok=True)
    tmp = usage.CACHE / f".{STATE_FILE}.{os.getpid()}"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, usage.CACHE / STATE_FILE)


def read_state() -> tuple[dt.datetime | None, list[Meter]]:
    try:
        data = json.loads((usage.CACHE / STATE_FILE).read_text())
        return dt.datetime.fromisoformat(data["computed_at"]), [Meter(**m) for m in data["meters"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def evaluate(settings: Settings, now: dt.datetime | None = None) -> tuple[list[UsageRecord], list[Meter]]:
    """This month's records (sessions, and runtime when a budget needs it) and every budget's meters; saves the
    state the hook reads."""
    now = now or dt.datetime.now().astimezone()
    month = session_records(settings, since=month_start(now))
    if any(b.runtime for b in settings.budgets()):
        month += runtime_records(settings, month_start(now))
    found = meters(month, settings.budgets(), now, settings.org)
    save_state(found, now)
    return month, found


def run_blockers(root: Path, capability: str) -> list[str]:
    """Why a run of `capability` in the repo at `root` must not start (empty: it may). One owner for `factory run`
    and the engine's check_budget."""
    try:
        quick = Settings.load(None, root)
        if not quick.budgets():
            return []  # nothing to hold the run to: no scan
        settings = load(root)
    except (config_mod.ConfigError, costconfig.CostConfigError, org_mod.OrgError) as exc:
        return [f"the budget config can't be read, so the run is held: {exc}"]
    project = config_mod.load(root).name if (root / config_mod.CONFIG_NAME).is_file() else usage.resolve_repo(str(root)).repo
    probe = run_probe(settings, project, capability)
    _, found = evaluate(settings)
    return [f"{m.sentence()} — hard limit; set in {m.source}" for m in blockers(found, probe)]


# ── Landed tasks: a task's branch merged as a PR ─────────────────────────────────────────────────────────────────

LANDED, NOT_LANDED, NOT_CHECKED = "landed", "not landed", "not checked"


def merged_branches(root: str, refresh_hours: float = LANDED_REFRESH_HOURS, now: dt.datetime | None = None,
                    gh: Callable[[str], list[dict] | None] | None = None) -> set[str] | None:
    """Head branches of the repo's merged PRs (cached in the usage store), or None when `gh` can't tell."""
    now = now or dt.datetime.now(dt.timezone.utc)
    con = usage.connect()
    try:
        row = con.execute("SELECT fetched_at, ok FROM pr_fetch WHERE root=?", (root,)).fetchone()
        fresh = row and (now - dt.datetime.fromisoformat(row[0])).total_seconds() < refresh_hours * 3600
        if not fresh:
            prs = (gh or _gh_merged)(root)
            if prs is not None:
                con.execute("DELETE FROM prs WHERE root=?", (root,))
                con.executemany("INSERT OR REPLACE INTO prs VALUES(?,?,?,?)",
                                [(root, p["headRefName"], p.get("number"), p.get("mergedAt")) for p in prs])
            con.execute("INSERT OR REPLACE INTO pr_fetch VALUES(?,?,?)", (root, now.isoformat(), int(prs is not None)))
            con.commit()
            row = (now.isoformat(), int(prs is not None))
        if not row[1]:
            return None
        return {b for (b,) in con.execute("SELECT branch FROM prs WHERE root=?", (root,))}
    finally:
        con.close()


def _gh_merged(root: str) -> list[dict] | None:
    try:
        out = subprocess.run(["gh", "pr", "list", "--state", "merged", "--limit", "1000", "--json",
                              "number,headRefName,mergedAt"], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def task_status(records: list[UsageRecord], roots: dict[str, str],
                merged: Callable[[str], set[str] | None] = merged_branches) -> dict[tuple[str, str], str]:
    """(project, task) -> landed | not landed | not checked: landed when any of its branches merged as a PR."""
    branches: dict[tuple[str, str], set[str]] = {}
    for r in records:
        if r.source == "session" and r.task != NO_TASK:
            branches.setdefault((r.project, r.task), set()).add(r.branch)
    cache: dict[str, set[str] | None] = {}
    out = {}
    for (project, task), names in branches.items():
        root = roots.get(project)
        if root not in cache:
            cache[root] = merged(root) if root else None
        done = cache[root]
        out[(project, task)] = NOT_CHECKED if done is None else LANDED if names & done else NOT_LANDED
    return out


@dataclass
class LandedRow:
    department: str
    landed: int = 0
    landed_cost: float = 0.0
    open: int = 0
    open_cost: float = 0.0
    unchecked: int = 0
    unchecked_cost: float = 0.0
    no_task_cost: float = 0.0

    @property
    def per_landed(self) -> float | None:
        return self.landed_cost / self.landed if self.landed else None


def cost_per_landed_task(records: list[UsageRecord], status: dict[tuple[str, str], str]) -> list[LandedRow]:
    """Per department: what landed tasks cost it, each task counted once in every department that spent on it."""
    rows: dict[str, LandedRow] = {}
    seen: dict[str, dict[str, set]] = {}
    for r in records:
        if r.source != "session":
            continue
        row = rows.setdefault(r.department, LandedRow(r.department))
        sets = seen.setdefault(r.department, {LANDED: set(), NOT_LANDED: set(), NOT_CHECKED: set()})
        if r.task == NO_TASK:
            row.no_task_cost += r.cost_usd
            continue
        st = status.get((r.project, r.task), NOT_CHECKED)
        sets[st].add((r.project, r.task))
        if st == LANDED:
            row.landed_cost += r.cost_usd
        elif st == NOT_LANDED:
            row.open_cost += r.cost_usd
        else:
            row.unchecked_cost += r.cost_usd
    for dept, row in rows.items():
        row.landed, row.open, row.unchecked = (len(seen[dept][s]) for s in (LANDED, NOT_LANDED, NOT_CHECKED))
    return sorted(rows.values(), key=lambda x: -(x.landed_cost + x.open_cost + x.unchecked_cost + x.no_task_cost))


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
    if agent == usage.INTERACTIVE:
        return "Interactive session"
    kind, _, name = agent.partition(":")
    return {"run": f"Factory run: {name}", "subagent": f"Subagent: {name}"}.get(kind, agent)


# ── Commands ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _root(path: Path | None) -> Path | None:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path or Path.cwd(), capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def _report(args, settings: Settings, now: dt.datetime) -> int:
    p = period(args.since, now)
    rows = session_records(settings, since=p.since, until=p.until)
    t = total(rows)
    by = args.by
    names = (lambda r: settings.org.name_of(r.department)) if by == "department" else by
    groups = group(rows, names)
    if args.json:
        print(json.dumps({
            "since": p.since.isoformat(), "until": p.until.isoformat(), "by": by, "total": t.as_dict(),
            "rows": [{"name": name, **g.as_dict()} for name, g in groups],
        }, indent=2))
        return 0
    print(f"Spend by {by}, {p.last}: {fmt_usd(t.cost)} · {t.calls:,} turns · {fmt_tokens(t.tokens)} tokens"
          " (USD at API list rates)")
    if not groups:
        print("No agent turns in this period.")
        return 0
    width = min(max(len(n or "—") for n, _ in groups), 48)
    print(f"{by:<{width}}  {'turns':>8}  {'tokens':>8}  {'cost':>12}  share")
    for name, g in groups:
        share = g.cost / t.cost * 100 if t.cost else 0
        print(f"{(name or '—')[:width]:<{width}}  {g.calls:>8,}  {fmt_tokens(g.tokens):>8}  {fmt_usd(g.cost):>12}  "
              f"{share:4.0f}% {'█' * round(share / 5)}")
    unpriced = sorted({r.model for r in rows if not r.priced})
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
            kind = ("alert only" if not m.hard else "hard: the product enforces it" if m.runtime
                    else "hard: stops the run" if m.window == "latest run" else "hard: stops factory runs")
            print(f"{m.sentence()} · {kind}")
    return 1 if any(m.state == OVER for m in found) else 0


def _org(args, settings: Settings) -> int:
    data = settings.org.as_dict()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{data['organization']['name']} — Direct = {settings.org.name_of(settings.org.direct)}")
    for d in data["organization"]["departments"]:
        owns = "; ".join(f"{k}: {', '.join(v)}" for k, v in d.items() if k in org_mod.MAP_KEYS and v)
        print(f"  {d['id']:<12} {d['name']:<16} {owns or '—'}")
    for project, maps in sorted(settings.org.project_maps.items()):
        for kind, names in maps.items():
            print(f"  {project}: {kind} " + ", ".join(f"{n} → {d}" for n, d in sorted(names.items())))
    return 0


def hook(now: dt.datetime | None = None, spawn: bool = True) -> str:
    """The SessionStart line(s): budgets at their danger line or over, from the saved state. Starts a background
    refresh when the state is stale. Never scans, never raises."""
    try:
        now = now or dt.datetime.now().astimezone()
        computed, found = read_state()
        lines = [f"factory budget: {m.sentence()}"
                 + ("; factory runs in it are stopped" if m.hard and not m.runtime and m.state == OVER else "")
                 for m in found if m.state in (OVER, NEAR)]
        if lines:
            lines.append("factory budget: details with `factory cost budgets`")
        try:
            stale_hours = Settings(costconfig.load_user(), org=org_mod.Org()).stale_hours
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
        evaluate(load())
    finally:
        lock.unlink(missing_ok=True)
    return 0


def main(argv: list[str]) -> int:
    actions = ("budgets", "page", "check", "hook", "refresh", "org")
    action = argv[0] if argv[:1] and argv[0] in actions else "report"
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
    ap.add_argument("--repo", type=Path, help="the repo whose .factory.toml must load (default: the current repo)")
    if action in ("report", "page"):
        ap.add_argument("--since", default="30d", help="7d, 30d, 24h, month, today or YYYY-MM-DD (default 30d)")
    if action == "report":
        ap.add_argument("--by", choices=BY, default="department")
    if action in ("report", "budgets", "org"):
        ap.add_argument("--json", action="store_true")
    if action == "page":
        ap.add_argument("--out", type=Path, help=f"default {usage.CACHE / 'costs.html'}")
        ap.add_argument("--open", action="store_true", help="open it in the browser")
        ap.add_argument("--no-landed", action="store_true", help="don't ask GitHub which tasks landed")
    if action == "check":
        ap.add_argument("--capability", required=True)
    args = ap.parse_args(rest)
    root = _root(args.repo)
    if action == "check":
        reasons = run_blockers(root or Path.cwd(), args.capability)
        for r in reasons:
            print(f"factory: {args.capability} run stopped by a budget: {r}")
        return 1 if reasons else 0
    now = dt.datetime.now().astimezone()
    try:
        settings = load(root)
    except (config_mod.ConfigError, costconfig.CostConfigError, org_mod.OrgError) as exc:
        raise SystemExit(f"factory cost: {exc}") from None
    if action == "report":
        return _report(args, settings, now)
    if action == "budgets":
        return _budgets(args, settings, now)
    if action == "org":
        return _org(args, settings)
    from . import costpage

    started = time.monotonic()
    out = args.out or usage.CACHE / "costs.html"
    p = period(args.since, now)
    span_start = min(p.prior_since, month_start(now))
    rows = session_records(settings, since=span_start)
    runtime = runtime_records(settings, span_start)
    month = [r for r in rows + runtime if r.ts >= month_start(now)]
    found = meters(month, settings.budgets(), now, settings.org)
    save_state(found, now)
    cur = [r for r in rows if r.ts >= p.since]
    status = {} if args.no_landed else task_status(cur, settings.roots)
    html = costpage.render(costpage.Model.build(rows, runtime, found, settings, p, now, status, landed=not args.no_landed))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"factory cost: page written to {out} ({time.monotonic() - started:.1f}s)")
    for note in settings.notes:
        print(f"note: {note}", file=sys.stderr)
    if args.open:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(out)], check=False)
    return 0
