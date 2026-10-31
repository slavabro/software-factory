"""`.factory.toml` — what a repo tells the factory about itself.

Every capability reads its own section; anything not set falls back to a default that works for a plain repo, so
the file only holds what is specific to the repo. Reference with every key: docs/factory-toml.md.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".factory.toml"

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "Skill", "Agent",
    "Bash(git *)", "Bash(gh *)", "Bash(make *)", "Bash(python3 *)", "Bash(npm *)", "Bash(npx *)",
    "Bash(factory *)", "Bash(skillcheck *)", "Bash(ls *)", "Bash(cat *)", "Bash(grep *)", "Bash(sed *)",
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
    new_task: str = ""  # shell command with {title} {body_file} {priority}; empty = an issue via `gh issue create`
    worktree_setup: list[str] = field(default_factory=list)  # run in a fresh worktree; $REPO = the main checkout
    agent_command: list[str] = field(
        default_factory=lambda: ["claude", "-p", "{prompt}", "--allowedTools", "{allowed_tools}"]
    )
    agent_allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    upgrade: Upgrade = field(default_factory=Upgrade)


def load(root: Path) -> Config:
    root = root.resolve()
    cfg = Config(root=root, name=root.name)
    path = root / CONFIG_NAME
    if not path.is_file():
        return cfg
    raw = tomllib.loads(path.read_text())
    repo = raw.get("repo", {})
    cfg.name = str(repo.get("name", cfg.name))
    cfg.default_branch = str(repo.get("default_branch", cfg.default_branch))
    cfg.verify = list(raw.get("verify", {}).get("commands", cfg.verify))
    cfg.land = str(raw.get("land", {}).get("with", cfg.land))
    cfg.new_task = str(raw.get("tasks", {}).get("new", cfg.new_task))
    cfg.worktree_setup = list(raw.get("worktree", {}).get("setup", cfg.worktree_setup))
    agent = raw.get("agent", {})
    cfg.agent_command = list(agent.get("command", cfg.agent_command))
    cfg.agent_allowed_tools = list(agent.get("allowed_tools", cfg.agent_allowed_tools))
    cfg.agent_allowed_tools += [t for t in agent.get("extra_allowed_tools", []) if t not in cfg.agent_allowed_tools]
    up = raw.get("upgrade", {})
    for key, value in up.items():
        if hasattr(cfg.upgrade, key):
            setattr(cfg.upgrade, key, value)
    return cfg
