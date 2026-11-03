"""`.factory.toml` — what a repo tells the factory about itself.

Every capability reads its own section; anything not set falls back to a default that works for a plain repo, so
the file only holds what is specific to the repo. Unknown keys are an error (a typo must not silently drop a
safety setting). Reference with every key: docs/factory-toml.md.
"""

from __future__ import annotations

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


@dataclass
class Config:
    root: Path
    name: str = ""
    default_branch: str = "main"
    verify: list[str] = field(default_factory=lambda: ["make ci"])  # proves the code still works
    land: str = "skill:land"  # "skill:<name>" or a shell command that opens the PR
    # Task filing. Placeholders are shell-quoted by `factory task …`: {title} {body_file} {priority} {query}.
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


class ConfigError(ValueError):
    pass


SECTIONS = {
    "repo": {"name", "default_branch"},
    "verify": {"commands"},
    "land": {"with"},
    "tasks": {"new", "exists"},
    "worktree": {"setup", "path"},
    "agent": {"command", "allowed_tools", "extra_allowed_tools", "disallowed_tools", "extra_disallowed_tools", "timeout_minutes"},
    "report": {"with"},
    "upgrade": {f.name for f in fields(Upgrade)},
}


def _check(raw: dict, path: Path) -> None:
    for section, body in raw.items():
        if section not in SECTIONS:
            raise ConfigError(f"{path}: unknown section [{section}] (known: {', '.join(sorted(SECTIONS))})")
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        for key in body:
            if key not in SECTIONS[section]:
                raise ConfigError(f"{path}: unknown key {section}.{key} (known: {', '.join(sorted(SECTIONS[section]))})")
    defaults = Upgrade()
    for key, value in raw.get("upgrade", {}).items():
        want = type(getattr(defaults, key))
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ConfigError(f"{path}: upgrade.{key} must be {want.__name__}, got {type(value).__name__}")


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
    return cfg
