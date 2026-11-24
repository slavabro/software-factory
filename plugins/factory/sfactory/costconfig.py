"""`[cost]`, `[org]` and `[[budgets]]` (and `[efficiency] runtime_usage`): the same sections in the user's
`~/.config/software-factory/factory.toml` (all spend; the place for the org) and in a repo's `.factory.toml` (that
repo's spend; its `[org]` only maps its own skills). Reference: docs/factory-toml.md."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import org as org_mod

USER_CONFIG = Path(os.environ.get("FACTORY_USER_CONFIG", "~/.config/software-factory/factory.toml")).expanduser()

COST_KEYS = {"task_pattern", "plan_monthly_usd", "stale_hours", "web_search_usd", "prices"}
# Budget vocabulary as a product's spending rules: monthly_spend_usd, daily_spend_usd, monthly_call_count; plus tokens
# and a cap on each single factory run.
SCOPES = ("department", "project", "agent", "task", "capability")
LIMITS = ("daily_spend_usd", "monthly_spend_usd", "monthly_call_count", "daily_tokens", "monthly_tokens", "run_spend_usd")
BUDGET_KEYS = {"name", "warn", "danger", "hard", "runtime", *SCOPES, *LIMITS}
SECTIONS = ("cost", "org", "budgets", "efficiency")
EFFICIENCY_KEYS = {"runtime_usage"}  # the efficiency scout (F-32) owns the rest of [efficiency]


class CostConfigError(ValueError):
    pass


@dataclass
class CostSettings:
    task_pattern: str | None = None  # None: inherit (user file, then usage.DEFAULT_TASK_PATTERN)
    plan_monthly_usd: float | None = None
    stale_hours: float | None = None
    web_search_usd: float | None = None
    prices: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class Budget:
    name: str = ""
    department: str | None = None
    project: str | None = None
    agent: str | None = None
    task: str | None = None
    capability: str | None = None  # a factory run's capability (or engine team)
    runtime: bool = False  # the product's runtime model calls instead of agent sessions
    daily_spend_usd: float = 0
    monthly_spend_usd: float = 0
    monthly_call_count: int = 0
    daily_tokens: int = 0
    monthly_tokens: int = 0
    run_spend_usd: float = 0  # each single run of `capability`
    warn: float = 75
    danger: float = 90
    hard: bool = False
    source: str = ""  # the file it came from

    @property
    def scope(self) -> list[tuple[str, str]]:
        return [(k, getattr(self, k)) for k in SCOPES if getattr(self, k) is not None]

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        parts = [f"{k} {v}" for k, v in self.scope]
        base = " · ".join(parts) if parts else "all spend"
        return f"{base} (runtime)" if self.runtime else base


def default_hard(b: Budget) -> bool:
    """Hard caps by default where they guard money: a project's spend, each factory run, and runtime spend.
    Department, agent and task budgets are steering targets: alert only, unless `hard = true`."""
    keys = {k for k, _ in b.scope}
    return b.runtime or "capability" in keys or keys == {"project"}


@dataclass
class CostConfig:
    settings: CostSettings = field(default_factory=CostSettings)
    org: dict = field(default_factory=dict)  # the raw [org] table; sfactory/org.py reads it
    budgets: list[Budget] = field(default_factory=list)
    runtime_usage: str = ""  # [efficiency] runtime_usage: the command printing the product's usage rows


def _num(value, where: str, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CostConfigError(f"{where} must be a number ≥ 0")
    if integer and not float(value).is_integer():
        raise CostConfigError(f"{where} must be a whole number")
    return int(value) if integer else float(value)


def parse(raw: dict, path: Path | str, owner_repo: str | None = None) -> CostConfig:
    """The cost sections of an already-loaded TOML document; other sections are the caller's. A repo's budgets
    (`owner_repo`) count only its own spend."""
    from .usage import PRICE_KEYS  # here, not at import: usage → runner → config → this module

    out = CostConfig()
    cost = raw.get("cost", {})
    if not isinstance(cost, dict):
        raise CostConfigError(f"{path}: [cost] must be a table")
    for key in cost:
        if key not in COST_KEYS:
            raise CostConfigError(f"{path}: unknown key cost.{key} (known: {', '.join(sorted(COST_KEYS))})")
    s = out.settings
    if "task_pattern" in cost:
        try:
            re.compile(str(cost["task_pattern"]))
        except re.error as exc:
            raise CostConfigError(f"{path}: cost.task_pattern is not a valid regex: {exc}") from None
        s.task_pattern = str(cost["task_pattern"])
    for key in ("plan_monthly_usd", "stale_hours", "web_search_usd"):
        if key in cost:
            setattr(s, key, _num(cost[key], f"{path}: cost.{key}"))
    prices = cost.get("prices", {})
    if not isinstance(prices, dict):
        raise CostConfigError(f"{path}: [cost.prices] must be a table of models")
    for model, keys in prices.items():
        if not isinstance(keys, dict):
            raise CostConfigError(f'{path}: [cost.prices."{model}"] must be a table')
        bad = set(keys) - set(PRICE_KEYS)
        if bad:
            raise CostConfigError(f"{path}: cost.prices.{model}: unknown {', '.join(sorted(bad))} (known: {', '.join(PRICE_KEYS)})")
        s.prices[model] = {k: _num(v, f"{path}: cost.prices.{model}.{k}") for k, v in keys.items()}

    org = raw.get("org", {})
    try:
        org_mod.check_section(org, str(path), repo=owner_repo is not None)
        if owner_repo is None:
            org_mod.parse_departments(org.get("departments", []), str(path))
        org_mod.parse_maps(org, str(path))
    except org_mod.OrgError as exc:
        raise CostConfigError(str(exc)) from None
    out.org = org

    eff = raw.get("efficiency", {})
    if not isinstance(eff, dict):
        raise CostConfigError(f"{path}: [efficiency] must be a table")
    bad = set(eff) - EFFICIENCY_KEYS
    if bad:
        raise CostConfigError(f"{path}: unknown key efficiency.{', efficiency.'.join(sorted(bad))} "
                              f"(known: {', '.join(sorted(EFFICIENCY_KEYS))})")
    if "runtime_usage" in eff:
        if not isinstance(eff["runtime_usage"], str):
            raise CostConfigError(f"{path}: efficiency.runtime_usage must be a command (a string)")
        out.runtime_usage = eff["runtime_usage"]

    budgets = raw.get("budgets", [])
    if not isinstance(budgets, list):
        raise CostConfigError(f"{path}: budgets are [[budgets]] entries")
    for i, body in enumerate(budgets, 1):
        out.budgets.append(_budget(body, f"{path}: budget {i}", str(path), owner_repo))
    return out


def _budget(body, where: str, source: str, owner_repo: str | None) -> Budget:
    if not isinstance(body, dict):
        raise CostConfigError(f"{where} must be a table")
    bad = set(body) - BUDGET_KEYS
    if bad:
        raise CostConfigError(f"{where}: unknown {', '.join(sorted(bad))} (known: {', '.join(sorted(BUDGET_KEYS))})")
    b = Budget(source=source)
    for key in ("name", *SCOPES):
        if key in body:
            if not isinstance(body[key], str) or not body[key]:
                raise CostConfigError(f"{where}.{key} must be a non-empty string")
            setattr(b, key, body[key])
    if owner_repo is not None:
        if b.project not in (None, owner_repo):
            raise CostConfigError(f"{where}: a repo's budget counts only its own spend (project = {owner_repo!r})")
        b.project = owner_repo
    others = [k for k in SCOPES if k != "project" and getattr(b, k) is not None]
    if len(others) > 1:
        raise CostConfigError(f"{where}: one of department, agent, task or capability at most, with or without "
                              f"project (got {', '.join(others)})")
    for key in LIMITS:
        if key in body:
            setattr(b, key, _num(body[key], f"{where}.{key}", key in ("monthly_call_count", "daily_tokens", "monthly_tokens")))
    if not any(getattr(b, k) for k in LIMITS):
        raise CostConfigError(f"{where}: set at least one of {', '.join(LIMITS)}")
    if b.run_spend_usd and b.capability is None:
        raise CostConfigError(f"{where}: run_spend_usd caps each run of a capability: set capability")
    if "runtime" in body:
        if not isinstance(body["runtime"], bool):
            raise CostConfigError(f"{where}.runtime must be true or false")
        b.runtime = body["runtime"]
        if b.runtime and (b.capability or b.agent and b.agent.startswith(("run:", "subagent:"))):
            raise CostConfigError(f"{where}: runtime spend has no factory runs or subagents")
    for key in ("warn", "danger"):
        if key in body:
            setattr(b, key, _num(body[key], f"{where}.{key}"))
    if not 0 < b.warn < b.danger <= 100:
        raise CostConfigError(f"{where}: needs 0 < warn < danger ≤ 100 (percent of the limit)")
    if "hard" in body:
        if not isinstance(body["hard"], bool):
            raise CostConfigError(f"{where}.hard must be true or false")
        b.hard = body["hard"]
    else:
        b.hard = default_hard(b)
    return b


def load_user(path: Path | None = None) -> CostConfig:
    """The user-level file; missing = nothing configured. Only these sections may appear in it."""
    path = USER_CONFIG if path is None else path
    if not path.is_file():
        return CostConfig()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise CostConfigError(f"{path}: {exc}") from None
    extra = set(raw) - set(SECTIONS) | ({"efficiency"} & set(raw))
    if extra:
        raise CostConfigError(f"{path}: unknown section(s) {', '.join(sorted(extra))} "
                              f"(known: {', '.join(s for s in SECTIONS if s != 'efficiency')})")
    return parse(raw, path)
