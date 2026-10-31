"""`.skillcheck.toml` — per-repo budgets, path checking, banned patterns and severity overrides.

Every value has a default from docs/RULES.md, so a repo with no config file still gets the full rule set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+; without it the defaults apply
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

CONFIG_NAME = ".skillcheck.toml"


@dataclass
class Banned:
    pattern: str
    message: str
    severity: str = "error"
    paths: list[str] = field(default_factory=lambda: ["**"])


@dataclass
class Config:
    root: Path
    skill_lines: int = 500
    skill_tokens: int = 5000
    reference_toc_lines: int = 100
    claude_md_lines: int = 200
    description_chars: int = 1024
    listing_chars: int = 8000
    duplicate_min_chars: int = 140
    check_paths: bool = True
    path_include: list[str] = field(default_factory=lambda: [".claude/**", "**/CLAUDE.md", "skills/**", "plugins/**"])
    path_ignore: list[str] = field(default_factory=list)
    path_allow: list[str] = field(default_factory=list)
    skip_dirs: list[str] = field(
        default_factory=lambda: [".git", "node_modules", ".venv", "venv", "__pycache__", "synced", ".next", "dist", "build"]
    )
    exclude: list[str] = field(default_factory=list)
    scan_include: list[str] = field(default_factory=list)  # extra docs to check (links, paths, banned), e.g. a testing guide
    banned: list[Banned] = field(default_factory=list)
    severity: dict[str, str] = field(default_factory=dict)  # rule -> error | warning | off
    fail_on: str = "error"  # error | warning


def load(root: Path, explicit: Path | None = None) -> Config:
    cfg = Config(root=root)
    path = explicit or root / CONFIG_NAME
    if not path.is_file() or tomllib is None:
        return cfg
    raw = tomllib.loads(path.read_text())
    for key, value in raw.get("budgets", {}).items():
        if hasattr(cfg, key):
            setattr(cfg, key, int(value))
    paths = raw.get("paths", {})
    cfg.check_paths = bool(paths.get("check", cfg.check_paths))
    cfg.path_include = list(paths.get("include", cfg.path_include))
    cfg.path_ignore = list(paths.get("ignore", cfg.path_ignore))
    cfg.path_allow = list(paths.get("allow", cfg.path_allow))
    cfg.exclude = list(raw.get("exclude", cfg.exclude))
    cfg.scan_include = list(raw.get("scan", {}).get("include", cfg.scan_include))
    cfg.skip_dirs = cfg.skip_dirs + list(raw.get("skip_dirs", []))
    cfg.banned = [Banned(**b) for b in raw.get("banned", [])]
    cfg.severity = {k.upper(): str(v) for k, v in raw.get("severity", {}).items()}
    cfg.fail_on = str(raw.get("fail_on", cfg.fail_on))
    return cfg
