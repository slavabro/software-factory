"""`.factory.toml` — what a repo tells the factory about itself.

Every capability reads its own section; anything not set falls back to a default that works for a plain repo, so
the file only holds what is specific to the repo. Unknown keys are an error (a typo must not silently drop a
safety setting). Reference with every key: docs/factory-toml.md.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_NAME = ".factory.toml"

# What an unattended agent may run: narrow on purpose. Merging, pushing the default branch and anything that
# runs arbitrary code by name (`python3 *`, `npx *`) are not on it; `{default_branch}` is filled in at run time.
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "Skill", "Agent", "TodoWrite",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git add *)",
    "Bash(git commit *)", "Bash(git checkout -b factory/*)", "Bash(git switch *)", "Bash(git branch*)",
    "Bash(git fetch*)", "Bash(git merge origin/{default_branch}*)", "Bash(git rev-parse*)", "Bash(git ls-remote*)",
    "Bash(git grep *)", "Bash(git restore *)", "Bash(git push -u origin factory/*)", "Bash(git push origin factory/*)", "Bash(git push -u origin HEAD*)",
    "Bash(git merge-base*)",
    "Bash(gh pr create *)", "Bash(gh pr list*)", "Bash(gh pr view*)", "Bash(gh pr checks*)", "Bash(gh pr edit *)",
    "Bash(gh run list*)", "Bash(gh run view*)", "Bash(gh run rerun *)", "Bash(gh issue create *)",
    "Bash(gh issue list*)", "Bash(gh api repos/*/releases*)", "Bash(gh release view *)", "Bash(gh release list *)",
    "Bash(factory *)", "Bash(skillcheck *)",
    "Bash(npm install *)", "Bash(npm ci*)", "Bash(npm run *)", "Bash(npm view *)", "Bash(npm outdated*)", "Bash(npm ls*)", "Bash(npm audit*)",
    "Bash(gh api /advisories*)", "Bash(gh api advisories*)",
    "Bash(make ci*)", "Bash(make lint*)", "Bash(make fmt*)", "Bash(make test*)",
    "Bash(ls *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(grep *)", "Bash(sed *)", "Bash(jq *)",
    "Bash(echo *)", "Bash(wc *)", "Bash(sort *)", "Bash(diff *)", "Bash(mktemp*)", "Bash(cd *)",
    "Bash(mkdir *)", "Bash(touch *)", "Bash(cp *)", "Bash(mv *)", "Bash(test *)", "Bash(printf *)", "Bash(true)",
]
# Refused even if an allow rule matches (Claude Code applies deny first). Pushes to the default branch are refused by
# the guard hook (sfactory/guard.py), which matches the refspec itself rather than a substring of the command.
DEFAULT_DISALLOWED_TOOLS = [
    "Bash(gh pr merge*)", "Bash(gh api *merge*)", "Bash(gh api -X*)", "Bash(gh api --method*)", "Bash(gh repo *)",
    "Bash(gh secret *)", "Bash(gh release create*)", "Bash(gh api graphql*)", "Bash(git push*--force*)",
    "Bash(git push*-f *)", "Bash(git push*--delete*)", "Bash(git reset --hard*)", "Bash(git stash*)",
    "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "Bash(sudo *)", "Bash(curl *|*sh*)",
]


@dataclass
class Upgrade:
    enabled: bool = True
    schedule: str = "0 6 * * 1"  # cron: minute hour day-of-month month day-of-week; "off" disables
    ecosystems: list[str] = field(default_factory=lambda: ["pip", "npm", "docker"])
    manifests: list[str] = field(default_factory=list)  # empty = auto-detect
    risky: list[str] = field(default_factory=list)  # globs of package names that become tasks, never PRs
    ignore: list[str] = field(default_factory=list)  # globs of package names never touched
    install: list[str] = field(default_factory=list)  # commands that install the changed manifests in the worktree
    deprecation_check: list[str] = field(default_factory=list)  # commands whose output lists deprecation warnings
    max_prs: int = 3  # per run


# Why a team is woken besides its schedule (engine events, docs/designs/engine.md).
WAKE_REASONS = ("ci_red", "deploy_failed", "incident", "merged")


@dataclass
class Team:
    """A standing team: an agent session the engine runs on this repo on a schedule and on events."""

    prompt: str = ""  # what the agent session is asked to do, e.g. "/factory:qa"
    schedule: str = "off"  # cron (UTC); "off" = events and manual runs only
    budget_minutes: int = 90  # one run's agent time across retries
    wake_on: list[str] = field(default_factory=list)  # events that wake it (WAKE_REASONS)


@dataclass
class Config:
    root: Path
    name: str = ""
    default_branch: str = "main"
    verify: list[str] = field(default_factory=lambda: ["make ci"])  # proves the code still works
    land: str = "skill:land"  # "skill:<name>" or a shell command that opens the PR
    # Tasks: "factory" = the factory's task system (docs/tasks/, `factory task`); "commands" = new/exists below.
    tasks_system: str = "factory"
    tasks_legacy_duplicate_ids: list[str] = field(default_factory=list)  # IDs that shipped twice before IDs were checked
    # With system = "commands". Placeholders are shell-quoted by `factory task …`: {title} {body_file} {priority} {query}.
    new_task: str = "gh issue create --title {title} --body-file {body_file}"
    task_exists: str = "gh issue list --state open --search {query} --json number --jq 'length > 0' | grep -q true"
    worktree_setup: list[str] = field(default_factory=list)  # run in a fresh worktree; $REPO = the main checkout
    worktree_path: list[str] = field(default_factory=list)  # worktree-relative dirs put first on PATH (e.g. .venv/bin)
    agent_command: list[str] = field(
        default_factory=lambda: [
            "claude", "-p", "{prompt}", "--allowedTools", "{allowed_tools}", "--disallowedTools", "{disallowed_tools}",
        ]
    )
    agent_allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    agent_disallowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_DISALLOWED_TOOLS))
    agent_timeout_minutes: int = 180
    report_with: str = ""  # shell command run with {file} = the run's digest; empty = path printed to the log
    upgrade: Upgrade = field(default_factory=Upgrade)
    extra_teams: dict[str, Team] = field(default_factory=dict)  # [teams.<name>]

    def teams(self) -> dict[str, Team]:
        """Every team the engine runs here. The upgrade capability is the team `upgrade`, configured by [upgrade]."""
        teams = dict(self.extra_teams)
        if self.upgrade.enabled:
            teams["upgrade"] = Team(
                prompt="/factory:upgrade", schedule=self.upgrade.schedule, budget_minutes=self.agent_timeout_minutes
            )
        return teams


class ConfigError(ValueError):
    pass


SECTIONS = {
    "repo": {"name", "default_branch"},
    "verify": {"commands"},
    "land": {"with"},
    "tasks": {"system", "new", "exists", "legacy_duplicate_ids"},
    "worktree": {"setup", "path"},
    "agent": {"command", "allowed_tools", "extra_allowed_tools", "disallowed_tools", "extra_disallowed_tools", "timeout_minutes"},
    "report": {"with"},
    "upgrade": {f.name for f in fields(Upgrade)},
    "teams": set(),  # one table per team, keys TEAM_KEYS
}
TEAM_KEYS = {f.name for f in fields(Team)}
TEAM_NAME = re.compile(r"[a-z][a-z0-9-]*")


def _check(raw: dict, path: Path) -> None:
    for section, body in raw.items():
        if section not in SECTIONS:
            raise ConfigError(f"{path}: unknown section [{section}] (known: {', '.join(sorted(SECTIONS))})")
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        if section == "teams":
            _check_teams(body, path)
            continue
        for key in body:
            if key not in SECTIONS[section]:
                raise ConfigError(f"{path}: unknown key {section}.{key} (known: {', '.join(sorted(SECTIONS[section]))})")
    defaults = Upgrade()
    for key, value in raw.get("upgrade", {}).items():
        want = type(getattr(defaults, key))
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ConfigError(f"{path}: upgrade.{key} must be {want.__name__}, got {type(value).__name__}")


def _check_teams(teams: dict, path: Path) -> None:
    defaults = Team()
    for name, body in teams.items():
        where = f"{path}: teams.{name}"
        if not TEAM_NAME.fullmatch(name):
            raise ConfigError(f"{where}: a team name is lowercase letters, digits and dashes")
        if name == "upgrade":
            raise ConfigError(f"{where}: the upgrade team is configured by [upgrade]")
        if not isinstance(body, dict):
            raise ConfigError(f"{where} must be a table")
        for key, value in body.items():
            if key not in TEAM_KEYS:
                raise ConfigError(f"{where}: unknown key {key} (known: {', '.join(sorted(TEAM_KEYS))})")
            want = type(getattr(defaults, key))
            if not isinstance(value, want) or (want is int and isinstance(value, bool)):
                raise ConfigError(f"{where}.{key} must be {want.__name__}, got {type(value).__name__}")
        if not body.get("prompt"):
            raise ConfigError(f"{where}: prompt is required")
        bad = [r for r in body.get("wake_on", []) if r not in WAKE_REASONS]
        if bad:
            raise ConfigError(f"{where}.wake_on: unknown {', '.join(bad)} (known: {', '.join(WAKE_REASONS)})")
        if body.get("budget_minutes", 1) < 1:
            raise ConfigError(f"{where}.budget_minutes must be at least 1")


def load(root: Path) -> Config:
    path = root.resolve() / CONFIG_NAME
    return parse(root, path.read_text() if path.is_file() else None, path)


def load_ref(root: Path, ref: str) -> Config:
    """The config as committed at `ref` (e.g. origin/main) — what a run will use, whatever the checkout holds."""
    shown = subprocess.run(["git", "show", f"{ref}:{CONFIG_NAME}"], cwd=root, capture_output=True, text=True)
    return parse(root, shown.stdout if shown.returncode == 0 else None, Path(f"{ref}:{CONFIG_NAME}"))


def parse(root: Path, text: str | None, path: Path) -> Config:
    root = root.resolve()
    cfg = Config(root=root, name=root.name)
    if text is None:
        return cfg
    raw = tomllib.loads(text)
    _check(raw, path)
    repo = raw.get("repo", {})
    cfg.name = str(repo.get("name", cfg.name))
    cfg.default_branch = str(repo.get("default_branch", cfg.default_branch))
    cfg.verify = list(raw.get("verify", {}).get("commands", cfg.verify))
    cfg.land = str(raw.get("land", {}).get("with", cfg.land))
    tasks = raw.get("tasks", {})
    cfg.tasks_system = str(tasks.get("system", "commands" if {"new", "exists"} & set(tasks) else cfg.tasks_system))
    if cfg.tasks_system not in ("factory", "commands"):
        raise ConfigError(f'{path}: tasks.system must be "factory" or "commands", got "{cfg.tasks_system}"')
    cfg.tasks_legacy_duplicate_ids = [str(i) for i in tasks.get("legacy_duplicate_ids", [])]
    cfg.new_task = str(tasks.get("new", cfg.new_task))
    cfg.task_exists = str(tasks.get("exists", cfg.task_exists))
    wt = raw.get("worktree", {})
    cfg.worktree_setup = list(wt.get("setup", cfg.worktree_setup))
    cfg.worktree_path = list(wt.get("path", cfg.worktree_path))
    agent = raw.get("agent", {})
    cfg.agent_command = list(agent.get("command", cfg.agent_command))
    cfg.agent_allowed_tools = list(agent.get("allowed_tools", cfg.agent_allowed_tools))
    cfg.agent_allowed_tools += [t for t in agent.get("extra_allowed_tools", []) if t not in cfg.agent_allowed_tools]
    cfg.agent_disallowed_tools = list(agent.get("disallowed_tools", cfg.agent_disallowed_tools))
    cfg.agent_disallowed_tools += [t for t in agent.get("extra_disallowed_tools", []) if t not in cfg.agent_disallowed_tools]
    cfg.agent_timeout_minutes = int(agent.get("timeout_minutes", cfg.agent_timeout_minutes))
    cfg.report_with = str(raw.get("report", {}).get("with", cfg.report_with))
    for key, value in raw.get("upgrade", {}).items():
        setattr(cfg.upgrade, key, value)
    cfg.extra_teams = {name: Team(**body) for name, body in raw.get("teams", {}).items()}
    return cfg
