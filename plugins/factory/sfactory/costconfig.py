"""`[cost]`, `[departments.*]` and `[[budgets]]` — the same three sections in a repo's `.factory.toml` (that repo's
spend) and in the user's `~/.config/software-factory/factory.toml` (all spend). Reference: docs/factory-toml.md."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

USER_CONFIG = Path(os.environ.get("FACTORY_USER_CONFIG", "~/.config/software-factory/factory.toml")).expanduser()

# A task ID in a branch or worktree name: "PROJ-12", "f-32" (letters, a dash, digits) or "1.60a" (dotted);
# never a run stamp ("upgrade-20261109-0600") or an agent id ("agent-a1a4…").
DEFAULT_TASK_PATTERN = r"(?i)(?<![a-z0-9.])([a-z]{1,5}-\d{1,5}[a-z]?|\d{1,3}\.\d{1,3}[a-z]?)(?![0-9.])"

COST_KEYS = {"task_pattern", "plan_monthly_usd", "stale_hours", "web_search_usd", "prices"}
DEPARTMENT_KEYS = ("repos", "capabilities", "branches", "cwds", "agents")
SCOPES = ("department", "agent", "task", "repo")
LIMITS = ("daily_usd", "monthly_usd", "daily_tokens", "monthly_tokens")
BUDGET_KEYS = {"name", "warn", "danger", "hard", *SCOPES, *LIMITS}
SECTIONS = ("cost", "departments", "budgets")


class CostConfigError(ValueError):
    pass


@dataclass
class CostSettings:
    task_pattern: str | None = None  # None: inherit (user file, then DEFAULT_TASK_PATTERN)
    plan_monthly_usd: float | None = None
    stale_hours: float | None = None
    web_search_usd: float | None = None
    prices: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class Department:
    name: str
    repos: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    cwds: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)


@dataclass
class Budget:
    name: str = ""
    department: str | None = None
    agent: str | None = None
    task: str | None = None
    repo: str | None = None
    daily_usd: float = 0
    monthly_usd: float = 0
    daily_tokens: int = 0
    monthly_tokens: int = 0
    warn: float = 75
    danger: float = 90
    hard: bool = True
    source: str = ""  # the file it came from
    owner_repo: str | None = None  # set for a repo's own budgets: they count only that repo's spend

    @property
    def scope(self) -> tuple[str, str] | None:
        for key in SCOPES:
            value = getattr(self, key)
            if value is not None:
                return key, value
        return None

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        scope = self.scope
        base = f"{scope[0]} {scope[1]}" if scope else "all spend"
        return f"{self.owner_repo}: {base}" if self.owner_repo and scope != ("repo", self.owner_repo) else base


@dataclass
class CostConfig:
    settings: CostSettings = field(default_factory=CostSettings)
    departments: list[Department] = field(default_factory=list)  # in file order: the first match wins
    budgets: list[Budget] = field(default_factory=list)


def _num(value, where: str, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CostConfigError(f"{where} must be a number ≥ 0")
    if integer and not float(value).is_integer():
        raise CostConfigError(f"{where} must be a whole number")
    return int(value) if integer else float(value)


def _globs(value, where: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise CostConfigError(f"{where} must be a list of globs")
    return list(value)


def parse(raw: dict, path: Path | str, owner_repo: str | None = None) -> CostConfig:
    """The three sections of an already-loaded TOML document; other sections are the caller's."""
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
            raise CostConfigError(f"{path}: [cost.prices.\"{model}\"] must be a table")
        bad = set(keys) - set(PRICE_KEYS)
        if bad:
            raise CostConfigError(f"{path}: cost.prices.{model}: unknown {', '.join(sorted(bad))} (known: {', '.join(PRICE_KEYS)})")
        s.prices[model] = {k: _num(v, f"{path}: cost.prices.{model}.{k}", k == "long_above") for k, v in keys.items()}

    departments = raw.get("departments", {})
    if not isinstance(departments, dict):
        raise CostConfigError(f"{path}: [departments.<name>] must be tables")
    for name, body in departments.items():
        where = f"{path}: departments.{name}"
        if not isinstance(body, dict):
            raise CostConfigError(f"{where} must be a table")
        bad = set(body) - set(DEPARTMENT_KEYS)
        if bad:
            raise CostConfigError(f"{where}: unknown {', '.join(sorted(bad))} (known: {', '.join(DEPARTMENT_KEYS)})")
        if not body:
            raise CostConfigError(f"{where}: give at least one of {', '.join(DEPARTMENT_KEYS)}")
        out.departments.append(Department(name, **{k: _globs(v, f"{where}.{k}") for k, v in body.items()}))

    budgets = raw.get("budgets", [])
    if not isinstance(budgets, list):
        raise CostConfigError(f"{path}: budgets are [[budgets]] entries")
    for i, body in enumerate(budgets, 1):
        where = f"{path}: budget {i}"
        if not isinstance(body, dict):
            raise CostConfigError(f"{where} must be a table")
        bad = set(body) - BUDGET_KEYS
        if bad:
            raise CostConfigError(f"{where}: unknown {', '.join(sorted(bad))} (known: {', '.join(sorted(BUDGET_KEYS))})")
        scopes = [k for k in SCOPES if k in body]
        if len(scopes) > 1:
            raise CostConfigError(f"{where}: one scope at most (got {', '.join(scopes)})")
        b = Budget(source=str(path), owner_repo=owner_repo)
        for key in ("name", *SCOPES):
            if key in body:
                if not isinstance(body[key], str) or not body[key]:
                    raise CostConfigError(f"{where}.{key} must be a non-empty string")
                setattr(b, key, body[key])
        for key in LIMITS:
            if key in body:
                setattr(b, key, _num(body[key], f"{where}.{key}", key.endswith("tokens")))
        if not any(getattr(b, k) for k in LIMITS):
            raise CostConfigError(f"{where}: set at least one of {', '.join(LIMITS)}")
        for key in ("warn", "danger"):
            if key in body:
                setattr(b, key, _num(body[key], f"{where}.{key}"))
        if not 0 < b.warn < b.danger <= 100:
            raise CostConfigError(f"{where}: needs 0 < warn < danger ≤ 100 (percent of the limit)")
        if "hard" in body:
            if not isinstance(body["hard"], bool):
                raise CostConfigError(f"{where}.hard must be true or false")
            b.hard = body["hard"]
        out.budgets.append(b)
    return out


def load_user(path: Path | None = None) -> CostConfig:
    """The user-level file; missing = nothing configured. Only the cost sections may appear in it."""
    path = USER_CONFIG if path is None else path
    if not path.is_file():
        return CostConfig()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise CostConfigError(f"{path}: {exc}") from None
    extra = set(raw) - set(SECTIONS)
    if extra:
        raise CostConfigError(f"{path}: unknown section(s) {', '.join(sorted(extra))} (known: {', '.join(SECTIONS)})")
    return parse(raw, path)
